"""List written Events by exact local source identities."""
from contextlib import ExitStack
import logging
import sqlite3

from .briefing_fallback import ReadState
from .source_refs import message_seq


_log = logging.getLogger(__name__)
_STATUS = {
    'invalid_source_selection': 400,
    'store_busy': 503,
    'source_record_unreadable': 500,
    'internal_error': 500,
}
_RECORD = (KeyError, TypeError, ValueError, AttributeError, IndexError)


def database_busy(exc):
    """True when SQLite could not get a lock. Does not read the message into a response."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    code = getattr(exc, 'sqlite_errorcode', None)
    busy = getattr(sqlite3, 'SQLITE_BUSY', 5)
    locked = getattr(sqlite3, 'SQLITE_LOCKED', 6)
    if code in (busy, locked):
        return True
    name = getattr(exc, 'sqlite_errorname', None)
    if name in {'SQLITE_BUSY', 'SQLITE_LOCKED', 'SQLITE_BUSY_RECOVERY',
                'SQLITE_BUSY_SNAPSHOT', 'SQLITE_BUSY_TIMEOUT'}:
        return True
    # Older interpreters only expose the fixed SQLite text.
    text = str(exc)
    return text == 'database is locked' or text == 'database is busy'


def response_for(result):
    """JSON body and HTTP status. Failures keep only status and code."""
    if isinstance(result, dict) and result.get('status') == 'ok' and isinstance(result.get('events'), list):
        return result, 200
    code = result.get('code') if isinstance(result, dict) else None
    if code not in _STATUS:
        code = 'internal_error'
    return {'status': 'failed', 'code': code}, _STATUS[code]


def failure_from_exception(exc):
    _log.warning('by-source failed: %s', type(exc).__name__)
    code = 'store_busy' if database_busy(exc) else 'internal_error'
    return response_for({'status': 'failed', 'code': code})


def events_for_sources(service, args):
    session = args.get('session_id') if isinstance(args, dict) else None
    message_ids = args.get('message_ids') if isinstance(args, dict) else None
    if (not isinstance(session, str) or not session.strip()
            or not isinstance(message_ids, list) or not 0 < len(message_ids) <= 10000
            or any(message_seq(value) is None for value in message_ids)):
        return {'status': 'failed', 'code': 'invalid_source_selection'}
    wanted = {message_seq(value) for value in message_ids}
    try:
        return _collect(service, session, wanted)
    except sqlite3.Error as exc:
        if database_busy(exc):
            _log.warning('by-source failed: %s', type(exc).__name__)
            return {'status': 'failed', 'code': 'store_busy'}
        raise


def _collect(service, session, wanted):
    result = []
    with ExitStack() as stack:
        state = ReadState(service, stack)
        aliases = _aliases(state)
        if aliases is None:
            return {'status': 'failed', 'code': 'source_record_unreadable'}
        for event in service.store.all_events():
            try:
                event_id = event.event_id
                title = event.summary
                if not isinstance(event_id, str) or event_id in aliases or event.merged_into:
                    continue
                sources = state.sources(event) or []
                if any(_matches(source, session, wanted) for source in sources):
                    if not isinstance(title, str):
                        continue
                    result.append({'event_id': event_id, 'title': title})
            except sqlite3.Error:
                raise
            except _RECORD as exc:
                _log.warning('by-source skipped event: %s', type(exc).__name__)
                continue
    return {'status': 'ok', 'events': result}


def _aliases(state):
    """Merged-away ids. An unreadable row fails the request: dropping it would list a hidden event."""
    try:
        rows = state.rows('memory_lifecycle.sqlite3', 'aliases', 'SELECT id FROM aliases')
    except _RECORD as exc:
        _log.warning('by-source alias unreadable: %s', type(exc).__name__)
        return None
    aliases = set()
    for row in rows:
        try:
            aliases.add(row[0])
        except _RECORD as exc:
            _log.warning('by-source alias unreadable: %s', type(exc).__name__)
            return None
    return aliases


def _matches(source, session, wanted):
    if not isinstance(source, dict) or source.get('session_id') != session:
        return False
    return bool(wanted.intersection((
        message_seq(source.get('user_message_id')),
        message_seq(source.get('assistant_message_id')))))
