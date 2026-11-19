"""Compact, source-backed result cards for scoped memory search."""
from datetime import datetime

from .knowledge import KnowledgeStore
from .memory_lifecycle import sources_in_context
from .privacy import Privacy, context_source, keywords
from .search_matching import normalized, source_phrase_pattern, source_sentences, source_text, text_matches
from .slice_recall import SliceRecall
from .turn_slices import TurnSliceStore


def _window(service, context):
    active = context if context is not None else service._live_turn or {}
    request = active.get('request_context', {})
    source = context_source(service, active) or {}
    session = source.get('session_id') or request.get('session_id')
    ids = request.get('context_message_ids')
    return session, request.get('context_start_id'), None if ids is None else set(ids), source.get('user_message_id')


def _stamp(value):
    try:
        return datetime.fromisoformat(value).astimezone()
    except (TypeError, ValueError):
        return None


def _in_range(stamp, interval):
    return interval is None or (stamp is not None and interval[0] <= stamp <= interval[1])


def _day(stamp):
    return stamp.date().isoformat() if stamp else None


def _out(total, items):
    return {'status': 'ok', 'total': total, 'items': items}


def _outside(message, start, ids):
    try:
        mid = int(message['message_id'])
    except (KeyError, TypeError, ValueError):
        return True
    return mid not in ids if ids is not None else not start or mid < int(start)


def _quote(query, messages, focus=None):
    excerpts = [line for message in messages for line in source_sentences(message.get('content') or '')]
    if not excerpts:
        return None
    terms = keywords(query)
    pattern = source_phrase_pattern(query)
    focus = normalized(focus or '')
    line = max(excerpts, key=lambda text: (
        bool(pattern and pattern.search(source_text(text))),
        *text_matches(query, [text], terms=terms), bool(focus and focus in normalized(text))))
    if len(line) <= 200:
        return line
    literal = pattern.search(source_text(line)) if pattern else None
    offset = literal.start() if literal else line.casefold().find(query.casefold())
    start = max(0, offset - 35)
    return line[start:start + 200]


def _slice_stamps(store):
    if not store.path.exists():
        return {}
    with store._db() as db:
        return {row[0]: _stamp(row[1]) for row in db.execute('SELECT slice_id,occurred_at FROM slice_source_times')}


def search_sources(service, query, limit, interval, context):
    sid, start, ids, anchor = _window(service, context)
    recall = SliceRecall(service.store.root, service.source_instance)
    store = TurnSliceStore(service.store.root)
    stamps = _slice_stamps(store)
    slice_ids = {key for key, stamp in stamps.items() if _in_range(stamp, interval)} if interval else None
    metadata = recall.match(query, session_id=sid, anchor_message_id=anchor,
                            context_start_id=start, context_message_ids=ids, purpose='search',
                            search_slice_ids=slice_ids)
    rankings = recall.trace(metadata['trace_id'])['rankings']
    policy = Privacy(service.store.root)
    items = []
    for rank in rankings:
        if not rank['in_candidate_pool']:
            continue
        source = store.get(rank['slice_id'], policy=policy)
        if not source or source['instance'] != service.source_instance or source['content_hash'] != rank['content_hash']:
            continue
        stamp = stamps.get(rank['slice_id'])
        if not _in_range(stamp, interval):
            continue
        messages = source['messages']
        if sid == source['session_id']:
            messages = [m for m in messages if _outside(m, start, ids) and (
                not anchor or _outside(m, anchor, None))]
        if messages:
            items.append({'slice_id': rank['slice_id'], 'date': _day(stamp), 'quote': _quote(query, messages)})
    return _out(len(items), items[:limit])


def locate_memories(service, query, pool, events, lexical, limit, interval, context):
    from .briefing import source_records

    sid, start, ids, _ = _window(service, context)
    active = context if context is not None else service._live_turn or {}
    request = active.get('request_context', {})
    seen = set(request.get('visible_event_ids') or ()) | set(active.get('presented_event_ids', ()))
    store = KnowledgeStore(service.store.root, service.source_instance)
    terms = keywords(query)
    policy = Privacy(service.store.root)
    ranked = []
    for eid, score in pool:
        event = events[eid]
        if eid in seen or policy.event_blocked(event.to_dict(), service.source_instance) or ((start or ids is not None)
                           and sources_in_context(source_records(service, event), sid, start, ids)):
            continue
        ranked.append(((*lexical.get(eid, (0, 0)), score), 'event_id', eid,
                       _stamp(event.event_time) or _stamp(event.ingest_time), event))
    knowledge_stamps = _slice_stamps(TurnSliceStore(service.store.root))
    knowledge_seen = set(request.get('visible_knowledge_ids') or ()) | set(active.get('presented_knowledge_ids', ()))
    for item in store.all():
        match = text_matches(query, [item['content'], *item['cues']], terms=terms)
        sources = item['sources']
        if (not any(match) or item['memory_id'] in knowledge_seen
                or sources_in_context(sources, sid, start, ids)):
            continue
        dates = [knowledge_stamps.get(source['slice_id']) for source in sources]
        dates = [date for date in dates if date is not None]
        if interval:
            dates = [date for date in dates if _in_range(date, interval)]
            if not dates:
                continue
        ranked.append(((*match, 0), 'knowledge_id', item['memory_id'], min(dates, default=None), item))
    ranked.sort(key=lambda row: row[0], reverse=True)
    items = []
    for _, id_key, item_id, date, item in ranked[:limit]:
        if id_key == 'event_id':
            result = service.read_source_event(item_id, truncate=False, include_private=False)
            focus = item.focus
        else:
            try:
                result = {'slices': store.read_sources(item['sources'])}
            except ValueError:
                result = {}
            focus = None
        if 'slices' in result:
            messages = [m for source in result['slices'] for m in source['messages']
                        if source['session_id'] != sid or _outside(m, start, ids)]
        else:
            sources = source_records(service, item) or []
            in_session = any(source['session_id'] == sid for source in sources)
            messages = [m for m in result.get('messages', [])
                        if not in_session or _outside(m, start, ids)]
        items.append({id_key: item_id, 'date': _day(date), 'quote': _quote(query, messages, focus)})
    return _out(len(ranked), items)
