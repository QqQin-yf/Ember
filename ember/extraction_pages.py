"""Source-backed page status and explicit, scoped extraction authoring sessions.

Maintenance uses the existing generation/tool receipt ledger without inventing
a user message, recall exposure or visible assistant reply.
"""
import json
from .generations import fingerprint
from .source_refs import message_seq
from .turn_slices import TurnSliceStore
from .extraction import Extraction
from .slice_policy import informative
from .event_slices import EventSliceLinks, sync_from_files

ALLOWED_TOOLS = frozenset({
    'get_memory_extraction_batch', 'finish_memory_extraction_group',
    'write_event', 'write_promise', 'write_memory_knowledge', 'review_memory_knowledge',
    'list_memory_knowledge', 'read_memory_source', 'search_memory_slices', 'read_memory_slices',
})


def scope(body):
    if not isinstance(body, dict) or not isinstance(body.get('session_id'), str) or not body['session_id'].strip():
        raise ValueError('session_id required')
    ids = body.get('assistant_message_ids')
    if ids is not None and (not isinstance(ids, list) or len(ids) > 10000 or
                            any(message_seq(value) is None for value in ids) or
                            len({str(value) for value in ids}) != len(ids)):
        raise ValueError('assistant_message_ids must be distinct source seqs')
    return body['session_id'], None if ids is None else {str(value) for value in ids}


def sources(service, session_id, ids, message_ids=None):
    store = TurnSliceStore(service.store.root)
    if not store.path.exists() or ids == set() or message_ids == set():
        return []
    sql = 'SELECT * FROM turn_slices WHERE instance=? AND session_id=?'
    args = [service.source_instance, session_id]
    if message_ids is not None:
        marks = ','.join('?' for _ in message_ids)
        sql += f' AND (user_message_id IN ({marks}) OR assistant_message_id IN ({marks}))'
        args.extend(sorted(message_ids))
        args.extend(sorted(message_ids))
    elif ids is not None:
        sql += ' AND assistant_message_id IN (' + ','.join('?' for _ in ids) + ')'
        args.extend(sorted(ids))
    with store._db() as conn:
        rows = [dict(row) for row in conn.execute(sql + ' ORDER BY saved_at,slice_id', args)]
    for row in rows:
        row['messages'] = json.loads(row.pop('messages_json'))
        row['user_is_system'] = bool(row['user_is_system'])
    return rows


def status(service, body):
    status_body = dict(body)
    message_ids = status_body.pop('message_ids', None)
    if message_ids is not None:
        if 'assistant_message_ids' in status_body:
            raise ValueError('choose message_ids or assistant_message_ids')
        scope(dict(status_body, assistant_message_ids=message_ids))
    session_id, ids = scope(status_body)
    selected = {str(value) for value in message_ids} if message_ids is not None else None
    rows = sources(service, session_id, ids, selected)
    queue = Extraction(service)
    queue.recover_outputs()
    sync_from_files(service.store.root, service.source_instance)
    owned = {row['slice_id'] for row in EventSliceLinks(service.store.root).all(service.source_instance)}
    with queue.db() as conn:
        done = queue.completed_sources(conn)
    items = []
    for row in rows:
        settled = row['slice_id'] in owned or (row['slice_id'], row['content_hash']) in done or not informative(row)
        items.append({'slice_id': row['slice_id'], 'assistant_message_id': int(row['assistant_message_id']),
                      'state': 'settled' if settled else 'pending'})
    known = {str(item['assistant_message_id']) for item in items}
    if message_ids is not None:
        known = {str(message['message_id']) for row in rows for message in row['messages']}
        unknown = sorted(selected - known, key=int)
    else:
        unknown = sorted(ids - known, key=int) if ids is not None else []
    pending = sum(item['state'] == 'pending' for item in items)
    return {'status': 'ok', 'items': items, 'pending_count': pending,
            'unknown_message_ids': [int(value) for value in unknown],
            'complete': not unknown and pending == 0}


def registered_runs(service):
    with service.generations._db() as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS extraction_runs(maintenance_id TEXT PRIMARY KEY,scope_json TEXT NOT NULL)')
        return {row[0] for row in conn.execute('SELECT maintenance_id FROM extraction_runs')}


def start(service, body):
    from .source_sync import sync_increment
    session_id, ids = scope(body)
    proposal_mode = body.get('mode') == 'proposals'
    if body.get('mode', 'raw') not in ('raw', 'proposals') or (proposal_mode and ids is not None):
        raise ValueError('invalid_extraction_mode')
    if proposal_mode:
        from .proposals import TOOLS
        allowed_tools = TOOLS
    else:
        allowed_tools = ALLOWED_TOOLS
    rid = body.get('maintenance_id')
    if not isinstance(rid, str) or not rid.startswith('extraction-') or len(rid) > 200:
        raise ValueError('extraction maintenance_id required')
    request_hash = fingerprint({'kind': 'proposals' if proposal_mode else 'extraction', 'session_id': session_id,
                                'assistant_message_ids': None if ids is None else sorted(ids, key=int)})
    with service._generation_lock:
        registered = registered_runs(service)
        previous = service.generations.generation(rid)
        if previous:
            if rid not in registered or previous['request_hash'] != request_hash:
                return {'status': 'failed', 'code': 'id_conflict'}
            if previous['state'] == 'running' and rid in service._generation_contexts:
                view = _view(service, service._generation_contexts[rid]['extraction_scope'])
                return {'status': 'ok', 'maintenance_id': rid, 'allowed_tools': sorted(allowed_tools),
                        'complete': view['complete'], 'pending_count': view['pending_count']}
            return {'status': 'failed', 'code': 'extraction_context_lost'}
        with service.generations._db() as conn:
            active = list(conn.execute("SELECT recall_id FROM generations WHERE state IN ('starting','running')"))
        for row in active:
            active_id = row['recall_id']
            if active_id in registered:
                if any(call['state'] != 'completed' for call in service.generations.calls_for(active_id)):
                    return {'status': 'failed', 'code': 'tool_outcome_unknown'}
                service.generations.interrupt(active_id)
                service._generation_contexts.pop(active_id, None)
            else:
                return {'status': 'failed', 'code': 'generation_busy'}
        with service.runs.db() as conn:
            if conn.execute("SELECT 1 FROM runs WHERE state IN ('running','finishing')").fetchone():
                return {'status': 'failed', 'code': 'generation_busy'}
        result = {'status':'ok'} if proposal_mode else sync_increment(service)
        if result.get('status') != 'ok':
            return result
        queue = Extraction(service)
        if not proposal_mode:
            queue.discover()
        selected = [] if proposal_mode else sources(service, session_id, ids)
        if ids is not None and ids - {str(row['assistant_message_id']) for row in selected}:
            return {'status': 'failed', 'code': 'extraction_sources_pending'}
        session = service.get_or_create_session(session_id)
        turn = max(session.turn_counter, service.generations.max_turn(session_id)) + 1
        selected_scope = {'session_id': session_id,
                          'assistant_message_ids': [int(row['assistant_message_id']) for row in selected],
                          'slice_ids': [row['slice_id'] for row in selected]}
        if proposal_mode:
            selected_scope['mode'] = 'proposals'
        with service.generations._db() as conn:
            conn.execute("INSERT INTO generations(recall_id,session_id,request_hash,turn,state) VALUES (?,?,?,?,'running')",
                         (rid, session_id, request_hash, turn))
            conn.execute('INSERT INTO extraction_runs VALUES (?,?)', (rid, json.dumps(selected_scope)))
        service._generation_contexts[rid] = {
            'session': session, 'turn': turn,
            'extraction_scope': selected_scope,
            'request_context': {'session_id': session_id,
                                'context_message_ids': [int(message['message_id']) for row in selected for message in row['messages']]},
        }
        view = _view(service, service._generation_contexts[rid]['extraction_scope'])
        return {'status': 'ok', 'maintenance_id': rid, 'allowed_tools': sorted(allowed_tools),
                'complete': view['complete'], 'pending_count': view['pending_count']}


def finish(service, body):
    rid = body.get('maintenance_id')
    if not isinstance(rid, str) or not rid.startswith('extraction-'):
        raise ValueError('maintenance_id required')
    with service._generation_lock:
        row = service.generations.generation(rid)
        if rid not in registered_runs(service) or not row:
            return {'status': 'failed', 'code': 'unknown_maintenance'}
        if row['state'] == 'done':
            return json.loads(row['end_result'])
        context = service._generation_contexts.get(rid)
        if not context or 'extraction_scope' not in context or row['state'] != 'running':
            return {'status': 'failed', 'code': 'extraction_context_lost'}
        calls = service.generations.calls_for(rid)
        if any(call['state'] != 'completed' for call in calls):
            return {'status': 'failed', 'code': 'tool_outcome_unknown'}
        result = _view(service, context['extraction_scope'])
        service.generations.finish(rid, 'done', fingerprint(body), result, None)
        service._generation_contexts.pop(rid, None)
        return result


def _view(service, selected):
    if selected.get('mode') != 'proposals':
        return status(service, selected)
    from .proposals import pending_count
    pending = pending_count(service, selected['session_id'], selected.get('review_id'))
    return {'status':'ok','complete':pending == 0,'pending_count':pending}


def scoped_arguments(service, context, name, arguments):
    """Enforce the page scope at the tool boundary, including skip operations."""
    selected = context['extraction_scope']
    if selected.get('mode') == 'proposals':
        from .proposals import TOOLS
        if name not in TOOLS:
            raise ValueError('extraction_tool_not_allowed')
        return dict(arguments)
    allowed = set(selected['slice_ids'])
    if name not in ALLOWED_TOOLS:
        raise ValueError('extraction_tool_not_allowed')
    args = dict(arguments)
    if name == 'get_memory_extraction_batch':
        return {'source_session_id': selected['session_id'],
                'assistant_message_ids': selected['assistant_message_ids']}
    if name in ('write_event', 'write_promise', 'write_memory_knowledge') and not args.get('source_slice_ids'):
        raise ValueError('extraction_requires_source_slices')
    if args.get('source_slice_ids') and not set(args['source_slice_ids']) <= allowed:
        raise ValueError('extraction_sources_outside_page')
    if name == 'finish_memory_extraction_group':
        group = Extraction(service).live_group(args['group_id'])
        group_ids = {row['slice_id'] for row in group['sources']}
        if args.get('part_id'):
            if not group_ids <= allowed:
                raise ValueError('extraction_sources_outside_page')
            return args
        ids = set(args.get('source_slice_ids') or (group_ids & allowed))
        if not ids or not ids <= group_ids & allowed:
            raise ValueError('extraction_sources_outside_page')
        args['source_slice_ids'] = sorted(ids)
    return args
