"""Durable CC write requests settled against the native turn that carried the receipt."""
import json
from uuid import uuid4

from . import source_client
from .cc_quote_matching import resolve_spans
from .event_mutation import SupersessionError
from .generations import fingerprint, identity
from .source_paths import QuoteSourceError, generation_messages, legacy_messages
from .source_refs import message_seq


def _setup(db):
    db.execute("""CREATE TABLE IF NOT EXISTS cc_deferred_writes (
        id TEXT PRIMARY KEY, cc_session TEXT NOT NULL, live_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        arguments TEXT, historical_id TEXT, anchor TEXT, state TEXT NOT NULL, result TEXT,
        UNIQUE(live_id,fingerprint))""")


def enqueue(service, context, arguments):
    # Validate replacement semantics before returning an accepted receipt.
    from .event_mutation import replacement_targets
    try:
        replacement_targets(service, arguments)
    except SupersessionError as exc:
        return exc.as_result()
    checksum = fingerprint(arguments)
    with service.generations._db() as db:
        _setup(db)
        db.execute("""INSERT OR IGNORE INTO cc_deferred_writes
          (id,cc_session,live_id,fingerprint,arguments,state) VALUES (?,?,?,?,?,'pending')""",
                   ('cc-write-'+uuid4().hex, context['cc_session'], context['cc_live_id'], checksum,
                    json.dumps(arguments, ensure_ascii=False)))
        row = db.execute('SELECT id,result FROM cc_deferred_writes WHERE live_id=? AND fingerprint=?',
                         (context['cc_live_id'], checksum)).fetchone()
    return json.loads(row['result']) if row['result'] else {
        'status': 'pending_source', 'cc_write_receipt': row['id'],
        'hint': '已接收。CC 同步本轮原文后自动定位并写入，无需再次调用。'}


def ordered_evidence(service, anchor, current_assistant, spans, focus):
    identities = {(row['role'], message_seq(row['message_id'])): row for row in
                  legacy_messages(service.turn_ledger, 'main') + generation_messages(service.generations, 'main')}
    registered = []
    checked_anchor = False
    try:
        for page in source_client.iter_source_branch('main', anchor, instance=service.source_instance):
            if not page:
                continue
            if not checked_anchor:
                if message_seq(page[0]['message_id']) != current_assistant or page[0]['role'] != 'assistant':
                    raise QuoteSourceError('source_path_unknown')
                checked_anchor = True
            for row in page:
                seq = message_seq(row['message_id'])
                known = identities.get((row['role'], seq))
                if known:
                    registered.append(dict(row, turn=known['turn'], _source_session_id='main',
                                           _source_message_id=seq, _source_recall_id=known.get('recall_id')))
            if len(spans) == 1 and _complete_ordered_quotes(registered, spans[0], focus):
                break
    except source_client.SourceUnavailable as exc:
        if exc.code != 'cc_history_limit':
            raise
    if not checked_anchor:
        raise QuoteSourceError('source_path_unknown')
    return resolve_spans(spans, focus, list(reversed(registered)))


def _complete_ordered_quotes(rows, span, focus):
    quotes = [span['start_quote'], focus, span['end_quote']]
    point = 0
    for row in reversed(rows):
        while point < len(quotes) and quotes[point] in row['content']:
            point += 1
        if point == len(quotes):
            return True
    return False


def _finish(service, receipt_id, result):
    with service.generations._db() as db:
        db.execute("""UPDATE cc_deferred_writes SET state='finished',arguments=NULL,result=?
                      WHERE id=?""", (json.dumps(result, ensure_ascii=False), receipt_id))
    return {'ok': True, 'outcome': result.get('status'), 'code': result.get('code'),
            'event_id': result.get('event_id')}


def _outcome(service, row):
    if row['result']:
        result = json.loads(row['result'])
        return {'ok': True, 'outcome': result.get('status'), 'code': result.get('code'),
                'event_id': result.get('event_id')}
    if row['state'] == 'writing':
        # An interrupted store write may already have produced an Event.
        existing = next((ev for ev in service.event_sources.records()
                         if ev.get('writer_tool_call_id') == row['id']), None)
        if existing and existing['event_id'] in service.store._events:
            return _finish(service, row['id'], {'status': 'created', 'event_id': existing['event_id']})
        return _finish(service, row['id'], {'status': 'failed', 'code': 'tool_outcome_unknown'})
    return None


def settle(service, body):
    receipt_id, cc_session, historical_id, anchor = (
        identity(body.get(key)) for key in ('receipt_id', 'cc_session', 'historical_recall_id', 'anchor'))
    if None in (receipt_id, cc_session, historical_id, anchor):
        return service._generation_error('invalid_arguments', 'CC receipt, session, history and anchor are required.')
    with service._generation_lock:
        with service.generations._db() as db:
            _setup(db)
            row = db.execute('SELECT * FROM cc_deferred_writes WHERE id=? AND cc_session=?',
                             (receipt_id, cc_session)).fetchone()
        if row is None:
            return service._generation_error('unknown_cc_write_receipt', 'Unknown deferred write receipt.')
        if row['historical_id'] and (row['historical_id'], row['anchor']) != (historical_id, anchor):
            return service._generation_error('cc_write_receipt_conflict', 'The receipt belongs to a different turn.')
        prior = _outcome(service, row)
        if prior is not None:
            return prior
        generation = service.generations.generation(historical_id)
        if (generation is None or generation['domain'] != 'cc' or generation['turn'] is None
                or generation['session_id'] != 'main' or generation['state'] not in ('done', 'error', 'cancelled')):
            return service._generation_error('cc_history_not_ready', 'Waiting for CC source registration.')
        with service.generations._db() as db:
            db.execute('UPDATE cc_deferred_writes SET historical_id=?,anchor=? WHERE id=?',
                       (historical_id, anchor, receipt_id))
        if generation['state'] != 'done':
            return _finish(service, receipt_id, {'status': 'failed', 'code': 'cc_quote_incomplete_turn'})
        arguments = json.loads(row['arguments'])
    # Network reads and matching do not hold the active generation lock.
    try:
        evidence = ordered_evidence(service, anchor, message_seq(generation['assistant_message_id']),
                                    arguments['quote_spans'], arguments['event']['focus'])
    except source_client.SourceUnavailable as exc:
        return service._generation_error(exc.code, 'Unable to read CC originals. Retry settlement.')
    except QuoteSourceError as exc:
        evidence = exc
    with service._generation_lock:
        with service.generations._db() as db:
            current = db.execute('SELECT * FROM cc_deferred_writes WHERE id=?', (receipt_id,)).fetchone()
        prior = _outcome(service, current)
        if prior is not None:
            return prior
        if isinstance(evidence, QuoteSourceError):
            return _finish(service, receipt_id, evidence.as_result())
        with service.generations._db() as db:
            db.execute("UPDATE cc_deferred_writes SET state='writing' WHERE id=?", (receipt_id,))
        try:
            result = service.write_validated_event(service.get_or_create_session('main'), generation['turn'], arguments,
                writer_recall_id=historical_id, writer_tool_call_id=receipt_id,
                cc_context={'resolved_evidence': evidence})
        except (QuoteSourceError, SupersessionError) as exc:
            result = exc.as_result()
        return _finish(service, receipt_id, result)
