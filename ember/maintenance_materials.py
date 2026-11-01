"""Phase-separated maintenance materials; consume frozen pools, never search."""
import json
from pathlib import Path
import sqlite3

from .generations import fingerprint
from .source_refs import message_seq
from .turn_slices import TurnSliceStore


class MaintenancePending(Exception):
    pass


EVENT_FIELDS = ('event_id', 'version_id', 'event_time', 'ingest_time', 'summary', 'core', 'significance', 'kind', 'due', 'status', 'log', 'change')


def events(root):
    path = Path(root) / 'events.jsonl'
    return {r['event_id']: r for r in (json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip())} if path.exists() else {}


def event_document(event):
    # In particular: no key_quotes, conversation body, raw_ref or live session.
    return {key: event[key] for key in EVENT_FIELDS if key in event}


def _read_db(path, sql, args=()):
    if not Path(path).exists():
        return []
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute(sql, args)]
    finally:
        db.close()


def source_reference(root, instance, event_id):
    rows = _read_db(Path(root) / 'turn_slices.sqlite3',
        'SELECT * FROM event_span_sources WHERE instance=? AND event_id=?', (instance, event_id))
    if not rows or rows[0]['state'] != 'ready':
        raise MaintenancePending('event_source_not_ready')
    return json.loads(rows[0]['reference_json'])


def _accepted_traces(root):
    rows = _read_db(Path(root) / 'generations.sqlite3',
                   "SELECT metadata_json FROM recall_snapshots WHERE state='ready'")
    return {meta['slice_recall']['trace_id'] for row in rows
            for meta in [json.loads(row['metadata_json'])]
            if isinstance(meta.get('slice_recall'), dict) and meta['slice_recall'].get('trace_id')}


def _legacy_frames(root):
    path = Path(root) / 'turn_ledger.jsonl'
    grouped = {}
    if not path.exists():
        return set()
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        # Identified generations must be proved by their committed snapshot.
        if row.get('operation_id'):
            continue
        key = (row.get('ember_session'), row.get('ember_turn'))
        value = grouped.setdefault(key, {'user': set(), 'reply': set()})
        for field, target in [('user_message_id', 'user'), ('assistant_message_id', 'reply')]:
            seq = message_seq(row.get(field))
            if seq:
                value[target].add(seq)
    return {(sid, turn, next(iter(v['user']))) for (sid, turn), v in grouped.items()
            if len(v['user']) == len(v['reply']) == 1}


def prepare(root, instance, event_id):
    event = events(root).get(event_id)
    if event is None:
        raise MaintenancePending('event_content_missing')
    from .knowledge import KnowledgeStore
    explicit = KnowledgeStore(root,instance).event_sources(event_id)
    if explicit is not None:
        return prepare_slice_event(root, instance, event, explicit)
    ref = source_reference(root, instance, event_id)
    if str(ref['version_id']) != str(event.get('version_id')):
        raise MaintenancePending('event_source_version_changed')
    frames = {row['turn']: row for row in ref['turn_messages']
              if any(lo <= row['turn'] <= hi for lo, hi in ref['source_turn_spans'])}
    accepted, legacy = _accepted_traces(root), _legacy_frames(root)
    pools = _read_db(Path(root) / 'slice_recall.sqlite3', 'SELECT metadata_json FROM queries WHERE instance=?', (instance,))
    candidates, coverage, excluded = {}, {}, []
    new_user_ids = {message_seq(r.get('user_message_id')) for r in frames.values()} - {None}
    new_reply_ids = {message_seq(r.get('assistant_message_id')) for r in frames.values()} - {None}
    for row in pools:
        trace = json.loads(row['metadata_json'])
        turn = trace.get('turn')
        frame = frames.get(turn)
        if (not frame or trace.get('session_id') != ref['source_session_id'] or
                message_seq(trace.get('anchor_message_id')) != message_seq(frame.get('user_message_id'))):
            continue
        if (trace['trace_id'] not in accepted and
                (trace['session_id'], turn, trace['anchor_message_id']) not in legacy):
            continue
        coverage.setdefault(str(turn), []).append({'trace_id': trace['trace_id'], 'status': trace['status']})
        for candidate in trace.get('candidate_pool', []):
            # A recalled slice from this new Event's span would leak new text
            # into phase one. Retain the original trace and record this exclusion.
            if candidate.get('session_id') == ref['source_session_id'] and (
                    candidate.get('user_message_id') in new_user_ids or
                    candidate.get('assistant_message_id') in new_reply_ids):
                excluded.append({'slice_id': candidate['slice_id'], 'reason': 'inside_new_event_span'})
                continue
            item = candidates.setdefault(candidate['slice_id'], {
                'candidate_id': candidate['slice_id'], 'content_hash': candidate['content_hash'],
                'span_memberships': candidate.get('span_memberships', []) + candidate.get('source_memberships', []), 'triggers': []})
            if item['content_hash'] != candidate['content_hash']:
                raise MaintenancePending('candidate_versions_conflict')
            trigger = {'turn': turn, 'user_message_id': frame.get('user_message_id'),
                       'trace_id': trace['trace_id']}
            if trigger not in item['triggers']:
                item['triggers'].append(trigger)
    return {'instance': instance, 'event_id': event_id, 'version_id': event['version_id'],
        'event_hash': fingerprint(event_document(event)), 'event': event_document(event),
        'session_id': ref['source_session_id'], 'candidates': list(candidates.values()),
        'coverage': coverage, 'missing_candidate_turns': [t for t in frames if str(t) not in coverage],
        'excluded_self_span': excluded}


def old_slice(root, candidate, instance):
    source = TurnSliceStore(str(root)).get(candidate['candidate_id'])
    if source is None or source['instance'] != instance or source['content_hash'] != candidate['content_hash']:
        raise MaintenancePending('candidate_source_changed_or_missing')
    return {'candidate_id': candidate['candidate_id'], 'messages': source['messages']}


def stage_one(root, material, candidates):
    return {'phase': 'event', 'new_event': material['event'],
            'candidates': [old_slice(root, c, material['instance']) for c in candidates]}


def stage_two(root, material, candidates):
    ref = (source_reference(root, material['instance'], material['event_id'])
           if material.get('source_kind')!='slices' else None)
    frames = {r['turn']: r for r in ref['turn_messages']} if ref else {}
    result = []
    for candidate in candidates:
        item = old_slice(root, candidate, material['instance'])
        item['new_turns'] = []
        seen = set()
        for trigger in candidate['triggers']:
            turn = trigger['turn']
            sid = trigger.get('session_id',material.get('session_id'))
            if (sid,turn) in seen:
                continue
            seen.add((sid,turn))
            frame = frames.get(turn, {}) if ref else trigger
            user, assistant = message_seq(frame.get('user_message_id')), message_seq(frame.get('assistant_message_id'))
            if not assistant or user != message_seq(trigger.get('user_message_id')):
                raise MaintenancePending('trigger_source_pending_completion')
            rows = _read_db(Path(root) / 'turn_slices.sqlite3',
                'SELECT * FROM turn_slices WHERE instance=? AND session_id=? AND assistant_message_id=?',
                (material['instance'], sid, assistant))
            rows = [r for r in rows if user is None or r['user_message_id'] == user]
            if len(rows) != 1:
                raise MaintenancePending('trigger_slice_missing_or_ambiguous')
            source = rows[0]
            item['new_turns'].append({'turn': turn, 'slice_id': source['slice_id'],
                'content_hash': source['content_hash'], 'messages': json.loads(source['messages_json'])})
        # Event membership is only a possible destination, never proof of a relation.
        library = events(root)
        item['possible_event_sources'] = []
        for membership in candidate['span_memberships']:
            event = library.get(membership['event_id'])
            if event and str(event.get('version_id')) == str(membership['version_id']):
                item['possible_event_sources'].append({'event': event_document(event),
                    'event_hash': fingerprint(event_document(event))})
        result.append(item)
    return {'phase': 'turn', 'candidates': result}


def prepare_slice_event(root, instance, event, sources):
    """A disjoint Event has exact sources, not a fabricated covering span."""
    from .knowledge import KnowledgeStore
    KnowledgeStore(root,instance).read_sources(sources)
    frames={(s['session_id'],s['user_message_id']):s for s in sources}
    accepted,legacy=_accepted_traces(root),_legacy_frames(root)
    pools=_read_db(Path(root)/'slice_recall.sqlite3','SELECT metadata_json FROM queries WHERE instance=?',(instance,))
    candidates,covered,excluded={},set(),[]
    own={s['slice_id'] for s in sources}
    for row in pools:
        trace=json.loads(row['metadata_json'])
        frame=frames.get((trace.get('session_id'),message_seq(trace.get('anchor_message_id'))))
        if not frame or (trace['trace_id'] not in accepted and
                (trace['session_id'],trace.get('turn'),trace.get('anchor_message_id')) not in legacy):
            continue
        covered.add(frame['slice_id'])
        for candidate in trace.get('candidate_pool',[]):
            if candidate['slice_id'] in own:
                excluded.append({'slice_id':candidate['slice_id'],'reason':'inside_new_event_sources'})
                continue
            item=candidates.setdefault(candidate['slice_id'],{'candidate_id':candidate['slice_id'],
                'content_hash':candidate['content_hash'],'span_memberships':candidate.get('span_memberships',[])+candidate.get('source_memberships',[]),
                'triggers':[]})
            if item['content_hash']!=candidate['content_hash']:
                raise MaintenancePending('candidate_versions_conflict')
            # Use a job-local turn key; preserve exact original session/message
            # identities so same-numbered turns in two sessions cannot collide.
            trigger={'turn':list(frames).index((frame['session_id'],frame['user_message_id']))+1,
                'session_id':frame['session_id'],'user_message_id':frame['user_message_id'],
                'assistant_message_id':frame['assistant_message_id'],'trace_id':trace['trace_id']}
            if trigger not in item['triggers']:
                item['triggers'].append(trigger)
    return {'instance':instance,'event_id':event['event_id'],'version_id':event['version_id'],
        'event_hash':fingerprint(event_document(event)),'event':event_document(event),'source_kind':'slices',
        'candidates':list(candidates.values()),'coverage':sorted(covered),
        'missing_candidate_sources':sorted(own-covered),'excluded_self_span':excluded}
