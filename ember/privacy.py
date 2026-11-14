"""Source-session privacy and authorization for model egress."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cached_property
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3

from .source_refs import message_seq


offline_sources = ContextVar('offline_private_sources', default=None)


@contextmanager
def offline_material(root, sources):
    token = offline_sources.set((root, sources))
    try:
        yield
    finally:
        offline_sources.reset(token)


class PrivateMaterial(ValueError):
    pass


class Privacy:
    def __init__(self, root):
        self.path = Path(root) / 'private.sqlite3'

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _connection(self.path) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS ranges (
                instance TEXT, session_id TEXT, first INTEGER, last INTEGER,
                opened_at REAL, UNIQUE(instance,session_id,first,last))""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS active_private
                ON ranges(instance,session_id) WHERE last IS NULL""")
            db.execute('CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
            with db:
                yield db

    @staticmethod
    def read_ranges(db):
        result = {}
        for instance, session, first, last in db.execute('SELECT instance,session_id,first,last FROM ranges'):
            result.setdefault((instance, session), []).append((first, last))
        return result

    @cached_property
    def ranges(self):
        with self.db() as db:
            return self.read_ranges(db)

    @cached_property
    def events(self):
        from .maintenance_materials import events
        return events(self.path.parent)

    @cached_property
    def frozen_sources(self):
        from .source_refs import EventSourceStore
        return {row['event_id']: row for row in EventSourceStore(self.path.parent).records()}

    @cached_property
    def slice_event_sources(self):
        import json
        from .knowledge import KnowledgeStore
        store = KnowledgeStore(self.path.parent)
        if not store.path.exists():
            return {}
        with store.db() as db:
            return {(row[0], row[1]): json.loads(row[2]) for row in db.execute(
                'SELECT instance,memory_id,sources_json FROM slice_memory_sources')}

    @cached_property
    def configuration(self):
        value = os.environ.get('EMBER_PRIVATE_CUTOVER')
        configured = None
        if value:
            try:
                configured = datetime.fromisoformat(value)
                if configured.utcoffset() is None:
                    configured = None
            except ValueError:
                pass
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE name='cutover'").fetchone()
            has_ranges = db.execute('SELECT 1 FROM ranges LIMIT 1').fetchone() is not None
            if row is None and not has_ranges and configured is not None:
                db.execute("INSERT OR IGNORE INTO settings VALUES ('cutover', ?)", (configured.isoformat(),))
                row = db.execute("SELECT value FROM settings WHERE name='cutover'").fetchone()
        if row:
            stored = datetime.fromisoformat(row[0])
            warning = None if configured == stored else ('cutover_mismatch' if value else 'cutover_env_missing')
            return stored, warning
        if has_ranges:
            return None, 'cutover_missing_with_ranges'
        return None, 'cutover_invalid' if value else 'cutover_unconfigured'

    def cutover(self):
        return self.configuration[0]

    def active(self, instance, session=None):
        return any((instance is None or inst == instance) and (session is None or sid == session)
                   and any(last is None for _, last in ranges)
                   for (inst, sid), ranges in self.ranges.items())

    def status(self, instance, session):
        with self.db() as db:
            row = db.execute('SELECT first,opened_at FROM ranges WHERE instance=? AND session_id=? AND last IS NULL',
                             (instance, session)).fetchone()
        result = {'status': 'ok', 'private': row is not None}
        if row:
            result['start_message_id'] = str(row[0])
            elapsed = datetime.now(timezone.utc).timestamp() - row[1]
            from zoneinfo import ZoneInfo
            local = ZoneInfo('Asia/Taipei')
            crossed_midnight = datetime.fromtimestamp(row[1], local).date() < datetime.now(local).date()
            if elapsed >= 8 * 3600 or crossed_midnight:
                result['reminder'] = 'Private mode has been active for several hours or since yesterday.'
        return result

    def change(self, instance, session, action, anchor, start=None):
        anchor = int(message_seq(anchor) or 0)
        first = int(message_seq(start) or anchor)
        if not 0 < first <= anchor < 2**63 or action not in ('open', 'close'):
            raise ValueError('private_source_identity_invalid')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT first,opened_at FROM ranges WHERE instance=? AND session_id=? AND last IS NULL',
                             (instance, session)).fetchone()
            if action == 'open':
                if row:
                    first = min(first, row[0])
                    db.execute('DELETE FROM ranges WHERE instance=? AND session_id=? AND last IS NULL',
                               (instance, session))
                db.execute('INSERT INTO ranges VALUES (?,?,?,NULL,?)',
                    (instance, session, first, row[1] if row else datetime.now(timezone.utc).timestamp()))
            elif row:
                db.execute('INSERT OR IGNORE INTO ranges VALUES (?,?,?,?,?)',
                           (instance, session, row[0], max(anchor, row[0]), row[1]))
                db.execute('DELETE FROM ranges WHERE instance=? AND session_id=? AND last IS NULL',
                           (instance, session))
        self.__dict__.pop('ranges', None)
        return self.status(instance, session)

    def widen(self, instance, session, anchor, start, expected):
        if not 0 < int(start) <= int(anchor):
            raise ValueError('private_source_identity_invalid')
        with self.db() as db:
            db.execute('UPDATE ranges SET first=min(first,?) WHERE instance=? AND session_id=? AND last IS NULL AND first=?',
                       (int(start), instance, session, int(expected)))
        self.__dict__.pop('ranges', None)
        return self.status(instance, session)

    @staticmethod
    def overlaps(db, instance, session, first, last):
        first, last = message_seq(first), message_seq(last)
        if not session:
            return False
        if not (first or last):
            return db.execute('SELECT 1 FROM ranges WHERE instance=? AND session_id=? AND last IS NULL',
                              (instance, session)).fetchone() is not None
        return db.execute("""SELECT 1 FROM ranges WHERE instance=? AND session_id=?
            AND first<=? AND (last IS NULL OR last>=?) LIMIT 1""",
            (instance, session, int(last or first), int(first or last))).fetchone() is not None

    def protect_sources(self, sources, *, db=None):
        if db is None:
            with self.db() as connection:
                connection.execute('BEGIN IMMEDIATE')
                self.protect_sources(sources, db=connection)
            return
        for source in sources:
            first = message_seq(source.get('user_message_id')) or message_seq(source.get('assistant_message_id'))
            last = message_seq(source.get('assistant_message_id'))
            if first:
                db.execute('INSERT OR IGNORE INTO ranges VALUES (?,?,?,?,?)',
                           (source['instance'], source['session_id'], first, last or first,
                            datetime.now(timezone.utc).timestamp()))

    def event_sources(self, event_id, instance):
        event = self.events.get(event_id)
        cutover = self.cutover()
        if cutover is not None and event:
            try:
                stamp = datetime.fromisoformat(event.get('material_updated_at') or event['ingest_time'])
                if stamp < cutover:
                    return [{'legacy': True}]
            except (KeyError, ValueError, TypeError):
                pass
        if cutover is not None and event and event.get('material_sources_unverified'):
            return [{'unverified': True}]
        sources = self.raw_event_sources(event_id, instance)
        return sources or ([{'unverified': True}] if cutover is not None else [])

    def entity_sources(self, entity, events, instance):
        cutover = self.cutover()
        if cutover is not None:
            try:
                if datetime.fromisoformat(entity.summary_at or entity.created_at) < cutover:
                    return [{'legacy': True}]
            except (TypeError, ValueError):
                pass
        if entity.summary_sources is not None:
            return entity.summary_sources or [{'unverified': True}]
        if cutover is not None:
            return [{'unverified': True}]
        return [source for event in events if entity.entity_id in event.entity_refs
                for source in self.event_sources(event.event_id, instance)]

    def authored_cue_sources(self, event_id, instance):
        if self.cutover() is None:
            return []
        sources = self.event_sources(event_id, instance)
        return [source if source.get('legacy') or source.get('unverified') else
                dict(source, authored_cue=True) for source in sources]

    def raw_event_sources(self, event_id, instance):
        ref = self.frozen_sources.get(event_id)
        if ref:
            if self.cutover() is not None and not ref.get('identity_verified'):
                return []
            return [dict(row, instance=instance, session_id=ref['source_session_id'])
                    for row in ref['turn_messages']]
        return [dict(row, instance=instance) for row in self.slice_event_sources.get((instance, event_id), [])]

    def event_blocked(self, event, instance):
        if self.cutover() is None and not self.path.exists():
            return bool(event.get('private'))
        return bool(event.get('private')) or any(
            self.blocked(source) for source in self.event_sources(event['event_id'], instance))

    def blocked(self, source):
        if source.get('legacy') or source.get('authored_cue'):
            return False
        first = message_seq(source.get('user_message_id'))
        last = message_seq(source.get('assistant_message_id'))
        if source.get('unverified') or (self.configuration[1] != 'cutover_unconfigured' and not source.get('query')
                and not (source.get('instance') and source.get('session_id') and (first or last))):
            return True
        ranges = self.ranges.get((source.get('instance'), source.get('session_id')), ())
        if not (first or last):
            return any(end is None for _, end in ranges)
        return any(start <= int(last or first) and (end is None or end >= int(first or last))
                   for start, end in ranges)


@contextmanager
def _connection(path):
    db = sqlite3.connect(path, timeout=2)
    try:
        yield db
    finally:
        db.close()


def seconds_until_eligible(source):
    delay = int(os.environ.get('EMBER_PRIVATE_DELAY_SECONDS', '3600'))
    if delay < 0:
        raise ValueError('private_delay_invalid')
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(source['saved_at'])).total_seconds()
    return max(0, delay - age)


@contextmanager
def authorize(root, sources=(), *, private=False, delayed=False, query_source=None,
              source_required=False):
    """Validate a short read snapshot before outbound transport starts."""
    sources = list(sources)
    if private:
        raise PrivateMaterial('private_material')
    policy = Privacy(root) if root is not None else None
    if source_required:
        if policy and policy.cutover() is None and any(source.get('legacy') for source in sources):
            raise PrivateMaterial('private_cutover_unavailable')
        if root is None or not sources or any(
                not source.get('legacy') and (
                    not source.get('instance') or not source.get('session_id') or
                    not (message_seq(source.get('user_message_id')) or
                         message_seq(source.get('assistant_message_id'))))
                for source in sources):
            raise PrivateMaterial('private_source_unverified')
    if root is None:
        yield
        return
    _ = policy.configuration
    with policy.db() as db:
        db.execute('BEGIN')
        policy.__dict__['ranges'] = policy.read_ranges(db)
        if query_source and (policy.active(query_source['instance'], query_source['session_id']) or
                             policy.blocked(dict(query_source, query=True))):
            raise PrivateMaterial('private_material')
        if any(policy.blocked(source) for source in sources):
            raise PrivateMaterial('private_material')
        if delayed and any(seconds_until_eligible(source) for source in sources):
            raise PrivateMaterial('private_delay')
    yield


def keywords(text):
    import re

    cjk = r'[⺀-鿿가-힯豈-﫿𠀀-𲎯]'
    terms = set()
    for token in re.findall(cjk + r'+|[^\W_]+', text.casefold()):
        units = re.split('(' + cjk + r'+)', token)
        for unit in filter(None, units):
            if re.fullmatch(cjk + '+', unit) and len(unit) > 1:
                terms.update(unit[i:i + 2] for i in range(len(unit) - 1))
            else:
                terms.add(unit)
    return terms


def context_source(service, context):
    context = context or {}
    session = context.get('session')
    if session is None:
        return None
    return dict(instance=service.source_instance, session_id=session.session_id,
                user_message_id=context.get('private_anchor') or context.get('request_context', {}).get('user_message_id'))


def tool_specs():
    return [{'type': 'function', 'function': {
        'name': 'set_private', 'description': 'Start, end, or check private mode for the current source session. Start begins at the current turn unless a start quote or time is provided.',
        'parameters': {'type': 'object', 'required': ['action'], 'additionalProperties': False,
            'properties': {'action': {'enum': ['start', 'end', 'status'], 'type': 'string'},
                'start_quote': {'type': 'string', 'minLength': 1},
                'start_time': {'type': 'string', 'description': 'ISO 8601 timestamp with timezone.'}}}}}]


def dispatch(service, args, session, anchor, *, branch_anchor=None):
    from . import source_client

    policy = Privacy(service.store.root)
    instance = service.source_instance
    action = args.get('action')
    quote, stamp = args.get('start_quote'), args.get('start_time')
    if not isinstance(session, str) or not session.strip():
        return {'status': 'failed', 'code': 'private_session_missing'}
    if not isinstance(action, str):
        return {'status': 'failed', 'code': 'invalid_private_action'}
    action = {'start': 'open', 'end': 'close'}.get(action, action)
    if action == 'status' and not (quote or stamp):
        return policy.status(instance, session)
    anchor = message_seq(anchor)
    if not anchor or int(anchor) >= 2**63 or action not in ('open', 'close') or (action == 'close' and (quote or stamp)):
        return {'status': 'failed', 'code': 'private_source_identity_invalid'}
    if action == 'open' and policy.cutover() is None:
        return {'status': 'failed', 'code': 'private_cutover_unconfigured'}
    if action == 'close' or not (quote or stamp):
        return policy.change(instance, session, action, anchor)

    # Activate immediately, then widen if the optional historical lookup succeeds.
    result = policy.change(instance, session, 'open', anchor)
    failure = 'private_quote_not_found' if quote else 'private_time_not_found'
    try:
        if quote and stamp:
            raise ValueError('private_multiple_boundaries')
        if quote and not isinstance(quote, str):
            raise ValueError('private_quote_invalid')
        since = datetime.fromisoformat(stamp) if stamp else None
        if since and since.utcoffset() is None:
            raise ValueError('private_time_requires_timezone')
        matches = []
        start = None
        for rows in source_client.iter_source_branch(session, branch_anchor or anchor, instance=instance):
            if quote:
                matches.extend(message_seq(row['message_id']) for row in rows if quote in row['content'])
                if len(matches) > 1:
                    failure = 'private_quote_ambiguous'
                    break
                continue
            stamps = source_client.fetch_source_timestamps(
                session, [message_seq(row['message_id']) for row in rows], instance=instance)
            if any(message_seq(row['message_id']) not in stamps for row in rows):
                failure = 'private_source_times_unavailable'
                break
            for row in rows:
                seq = message_seq(row['message_id'])
                if stamps[seq] >= since.timestamp() * 1000:
                    start = seq
            if any(stamps[message_seq(row['message_id'])] < since.timestamp() * 1000 for row in rows):
                break
        if quote and len(matches) == 1:
            start = matches[0]
        if start and failure != 'private_source_times_unavailable':
            return policy.widen(instance, session, anchor, start, result['start_message_id'])
    except source_client.SourceUnavailable as exc:
        failure = exc.code
    except (ValueError, TypeError) as exc:
        failure = str(exc) if str(exc).startswith('private_') else 'private_time_invalid'
    return dict(policy.status(instance, session), backdate='failed', backdate_reason=failure)
