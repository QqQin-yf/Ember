"""List written Events by exact local source identities."""
from contextlib import ExitStack

from .briefing_fallback import ReadState
from .source_refs import message_seq


def events_for_sources(service, args):
    session = args.get('session_id') if isinstance(args, dict) else None
    message_ids = args.get('message_ids') if isinstance(args, dict) else None
    if (not isinstance(session, str) or not session.strip()
            or not isinstance(message_ids, list) or not 0 < len(message_ids) <= 10000
            or any(message_seq(value) is None for value in message_ids)):
        return {'status': 'failed', 'code': 'invalid_source_selection'}
    wanted = {message_seq(value) for value in message_ids}
    result = []
    with ExitStack() as stack:
        state = ReadState(service, stack)
        aliases = {row[0] for row in state.rows(
            'memory_lifecycle.sqlite3', 'aliases', 'SELECT id FROM aliases')}
        for event in service.store.all_events():
            if event.event_id in aliases or event.merged_into:
                continue
            sources = state.sources(event) or []
            if any(source['session_id'] == session and wanted.intersection(
                    (message_seq(source.get('user_message_id')),
                     message_seq(source.get('assistant_message_id')))) for source in sources):
                result.append({'event_id': event.event_id, 'title': event.summary})
    return {'status': 'ok', 'events': result}
