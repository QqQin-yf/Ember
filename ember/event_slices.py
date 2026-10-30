"""Derived many-to-many span membership using exact frozen message identities."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from .generations import fingerprint
from .source_completion import _project
from .source_refs import event_reference, message_seq
from .turn_slices import TurnSliceStore


def _spans(value):
    if not isinstance(value, list) or not value:
        return None
    if any(not isinstance(p, (list, tuple)) or len(p) != 2 or
           any(type(n) is not int or n < 1 for n in p) or p[0] > p[1] for p in value):
        return None
    return [list(p) for p in value]


class EventSliceLinks:
    def __init__(self, root):
        self.store = TurnSliceStore(str(root))

    @contextmanager
    def db(self):
        with self.store._db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS event_span_sources (
                instance TEXT NOT NULL, event_id TEXT NOT NULL, version_id TEXT,
                session_id TEXT, state TEXT NOT NULL, reason TEXT, source_hash TEXT NOT NULL,
                reference_json TEXT NOT NULL, PRIMARY KEY(instance,event_id));
                CREATE TABLE IF NOT EXISTS event_slice_memberships (
                instance TEXT NOT NULL, event_id TEXT NOT NULL, version_id TEXT NOT NULL,
                slice_id TEXT NOT NULL, turn INTEGER NOT NULL,
                span_start INTEGER NOT NULL, span_end INTEGER NOT NULL, source_hash TEXT NOT NULL,
                PRIMARY KEY(instance,event_id,version_id,slice_id,turn));''')
            yield db

    @staticmethod
    def _reason(ref):
        if not ref.get('event_id') or not ref.get('version_id') or not ref.get('source_session_id'):
            return 'source_identity_missing'
        if ref.get('source_evidence') not in ('frozen_messages', 'generation_completed'):
            return 'source_identity_unverified'
        if _spans(ref.get('source_turn_spans')) is None:
            return 'source_span_missing'
        rows = ref.get('turn_messages')
        if not isinstance(rows, list) or not rows:
            return 'source_identity_missing'
        turns, replies = set(), set()
        for row in rows:
            turn = row.get('turn')
            if type(turn) is not int or turn < 1 or turn in turns:
                return 'source_identity_ambiguous'
            turns.add(turn)
            for field in ('user_message_id', 'assistant_message_id'):
                if row.get(field) is not None and message_seq(row[field]) is None:
                    return 'source_identity_invalid'
            reply = message_seq(row.get('assistant_message_id'))
            if reply and reply in replies:
                return 'source_identity_ambiguous'
            if reply:
                replies.add(reply)
        return None

    def register(self, ref, instance='default', reason=None):
        reason = reason or self._reason(ref)
        digest = fingerprint(ref)
        with self.db() as db:
            old = db.execute('SELECT source_hash,state,reason FROM event_span_sources WHERE instance=? AND event_id=?',
                             (instance, ref['event_id'])).fetchone()
            state = 'unresolved' if reason else 'ready'
            if old is None or (old['source_hash'], old['state'], old['reason']) != (digest, state, reason):
                db.execute('DELETE FROM event_slice_memberships WHERE instance=? AND event_id=?',
                           (instance, ref['event_id']))
                db.execute('''INSERT INTO event_span_sources VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(instance,event_id) DO UPDATE SET version_id=excluded.version_id,
                    session_id=excluded.session_id,state=excluded.state,reason=excluded.reason,
                    source_hash=excluded.source_hash,reference_json=excluded.reference_json''',
                    (instance, ref['event_id'], ref.get('version_id'), ref.get('source_session_id'),
                     state, reason, digest, json.dumps(ref, ensure_ascii=False)))
            if not reason:
                slices = db.execute('SELECT * FROM turn_slices WHERE instance=? AND session_id=?',
                                    (instance, ref['source_session_id'])).fetchall()
                self._link(db, ref, digest, instance, slices)
        return {'event_id': ref['event_id'], 'state': state, 'reason': reason}

    @staticmethod
    def _link(db, ref, digest, instance, slices):
        by_reply = {}
        for source in slices:
            by_reply.setdefault(source['assistant_message_id'], []).append(source)
        for message in ref['turn_messages']:
            reply = message_seq(message.get('assistant_message_id'))
            if reply is None:
                continue  # A user identity alone cannot select a regenerated reply.
            turn = message['turn']
            span = next((p for p in ref['source_turn_spans'] if p[0] <= turn <= p[1]), None)
            if span is None:
                continue
            user = message_seq(message.get('user_message_id'))
            for source in by_reply.get(reply, []):
                if user is not None and source['user_message_id'] != user:
                    continue
                db.execute('INSERT OR IGNORE INTO event_slice_memberships VALUES (?,?,?,?,?,?,?,?)',
                    (instance, ref['event_id'], str(ref['version_id']), source['slice_id'],
                     turn, span[0], span[1], digest))

    def reconcile_slice(self, source):
        with self.db() as db:
            refs = db.execute("SELECT * FROM event_span_sources WHERE instance=? AND session_id=? AND state='ready'",
                              (source['instance'], source['session_id'])).fetchall()
            for row in refs:
                self._link(db, json.loads(row['reference_json']), row['source_hash'], source['instance'], [source])

    def all(self, instance='default'):
        if not self.store.path.exists():
            return []
        with self.db() as db:
            rows = db.execute('''SELECT m.*,s.reference_json FROM event_slice_memberships m
                JOIN event_span_sources s ON m.instance=s.instance AND m.event_id=s.event_id
                    AND m.version_id=s.version_id AND m.source_hash=s.source_hash
                JOIN turn_slices t ON m.slice_id=t.slice_id AND m.instance=t.instance
                WHERE m.instance=? AND s.state='ready' ORDER BY m.event_id,m.turn,m.slice_id''', (instance,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item['summary'] = json.loads(item.pop('reference_json')).get('summary', '')
            item['membership_kind'] = 'span'
            result.append(item)
        return result

    def for_slice(self, slice_id, instance='default'):
        return [row for row in self.all(instance) if row['slice_id'] == slice_id]


def register_snapshot(root, snapshot, instance=None):
    instance = instance or snapshot.get('instance', 'default')
    sources, spans = {}, {}
    for row in snapshot.get('event_sources', []):
        sources.setdefault(row['event_id'], row)
    for row in snapshot.get('turn_spans', []):
        spans[row['event_id']] = row.get('spans') or [[row.get('turn_start'), row.get('turn_end')]]
    bindings = {(r['source_hash'], r['turn']): json.loads(r['binding_json'])
                for r in snapshot.get('completion_bindings', [])}
    links = EventSliceLinks(root)
    results = []
    for event in snapshot.get('events', []):
        source = sources.get(event['event_id'])
        ref = event_reference(SimpleNamespace(**event), 'span_membership', SimpleNamespace(get=lambda _: source),
                              spans.get(event['event_id'], []))
        reason = None
        if source:
            if str(event.get('version_id')) != str(source.get('version_id')):
                reason = 'source_version_mismatch'
            event_sid = (event.get('source_ref') or '').removeprefix('conversation/session-').split('/')[0]
            if event_sid != source.get('source_session_id'):
                reason = 'source_session_mismatch'
            if event['event_id'] in spans and _spans(spans[event['event_id']]) != _spans(source.get('source_turn_spans')):
                reason = 'span_source_mismatch'
            ref = _project(ref, source, bindings)
        ref['summary'] = event.get('summary') or ''
        results.append(links.register(ref, instance, reason))
    return results


def sync_from_files(root, instance='default'):
    root = Path(root)
    events_path = root / 'events.jsonl'
    if not events_path.exists():
        return []
    def rows(name):
        path = root / name
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()] if path.exists() else []
    events = {r['event_id']: r for r in rows('events.jsonl')}
    bindings = []
    dbpath = root / 'generations.sqlite3'
    if dbpath.exists():
        db = sqlite3.connect(dbpath.resolve().as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_completions'").fetchone():
                bindings = [dict(r) for r in db.execute('SELECT * FROM source_completions')]
        finally:
            db.close()
    return register_snapshot(root, {'events': list(events.values()), 'event_sources': rows('event_sources.jsonl'),
        'turn_spans': rows('turn_spans.jsonl'), 'completion_bindings': bindings}, instance)
