"""Read-only snapshot plus recent additions, without refresh or model work."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3

from .briefing import get
from .lifecycle_config import lifecycle_config
from .memory_lifecycle import sources_in_context
from .promises import instant
from .source_completion import _project
from .source_refs import event_reference


NOTICE = '以下为上次简报之后的新记忆，简报尚未完成刷新'
# A conflicting writer usually holds the lock only for a commit. 300ms rides
# that out. by-source's caller stops at 5s, and one read may open several
# files, so a longer wait would look like a dropped call instead of store_busy.
# Zero fails on any in-flight commit.
READ_LOCK_WAIT_SECONDS = 0.3
_log = logging.getLogger(__name__)


def _skip(kind, exc):
    """Record the exception type only. Messages can carry paths or row text."""
    name = type(exc).__name__ if isinstance(exc, BaseException) else exc
    _log.warning("source read skipped %s: %s", kind, name)


class ReadState:
    """Local SQLite reads only; absent databases stay absent.

    A busy file is waited on briefly, then the read fails.
    """
    def __init__(self, service, stack):
        self.service, self.stack = service, stack
        self.connections = {}
        self.tables = {}
        self.bindings = None

    def rows(self, name, table, sql, params=()):
        if name not in self.connections:
            path = Path(self.service.store.root, name)
            conn = None
            if path.exists():
                conn = sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True,
                                       timeout=READ_LOCK_WAIT_SECONDS)
                self.stack.callback(conn.close)
                conn.row_factory = sqlite3.Row
                conn.execute('PRAGMA query_only=ON')
                conn.execute('BEGIN')
            self.connections[name] = conn
        conn = self.connections[name]
        if conn is None:
            return []
        if (name, table) not in self.tables:
            self.tables[name, table] = bool(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())
        if not self.tables[name, table]:
            return []
        return conn.execute(sql, params).fetchall()

    def sources(self, event):
        merged = self.rows('pin_policy.sqlite3', 'merged',
                           'SELECT source_ids FROM merged WHERE event_id=?', (event.event_id,))
        try:
            members = self._member_ids(merged, event)
        except sqlite3.Error:
            raise
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            _skip('merge', exc)
            return None
        if members is None:
            return None
        sources = []
        for eid in members:
            try:
                part = self._member_sources(eid)
            except sqlite3.Error:
                raise
            except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
                # One member of a merge can be half-written. Keep the others.
                _skip('member', exc)
                continue
            if part is None:
                return None
            sources.extend(part)
        return sources

    def _member_ids(self, merged, event):
        if not merged:
            return [event.event_id]
        parsed = json.loads(merged[0][0])
        if not parsed:
            return [event.event_id]
        if (not isinstance(parsed, list) or any(
                not isinstance(item, str) or not item for item in parsed)):
            _skip('merge', 'incomplete')
            return None
        return parsed

    def _member_sources(self, eid):
        explicit = self.rows('knowledge.sqlite3', 'slice_memory_sources',
            'SELECT sources_json FROM slice_memory_sources WHERE instance=? AND memory_id=?',
            (self.service.source_instance, eid))
        if explicit:
            refs = json.loads(explicit[0][0])
            return self._explicit_refs(refs)
        original = self.service.store.get_event(eid)
        ref = event_reference(original, 'expanded', self.service.event_sources,
                              self.service.turn_spans.span_of_event(original))
        messages = ref['turn_messages']
        if not isinstance(messages, list):
            return None
        if any(not isinstance(item, dict) for item in messages):
            _skip('turn-message', 'incomplete')
            messages = [item for item in messages if isinstance(item, dict)]
            if not messages:
                return None
            ref = dict(ref, turn_messages=messages)
        if any(item.get('assistant_message_id') is None for item in messages):
            if self.bindings is None:
                self._load_bindings()
            ref = _project(ref, self.service.event_sources.get(eid), self.bindings)
        if ref['source_evidence'] not in ('frozen_messages', 'generation_completed') or not ref['turn_messages']:
            return None
        session_id = ref['source_session_id']
        if not isinstance(session_id, str):
            return None
        usable = []
        for row in ref['turn_messages']:
            if not isinstance(row, dict):
                _skip('turn-message', 'incomplete')
                continue
            usable.append(dict(row, session_id=session_id))
        return usable

    def _explicit_refs(self, refs):
        if not isinstance(refs, list):
            _skip('slice-sources', 'incomplete')
            return None
        usable = []
        for ref in refs:
            # A slice that was stored without its id or hash cannot be checked.
            # Leave verified siblings in place. A hash mismatch still rejects
            # the whole event, same as a changed slice.
            if not isinstance(ref, dict) or 'slice_id' not in ref or 'content_hash' not in ref:
                _skip('slice-ref', 'incomplete')
                continue
            saved = self.rows('turn_slices.sqlite3', 'turn_slices',
                'SELECT instance,content_hash FROM turn_slices WHERE slice_id=?', (ref['slice_id'],))
            if (not saved or saved[0]['instance'] != self.service.source_instance
                    or saved[0]['content_hash'] != ref['content_hash']):
                return None
            usable.append(ref)
        return usable

    def _load_bindings(self):
        rows = self.rows(Path(self.service.generations.path).name, 'source_completions',
                         'SELECT * FROM source_completions')
        bindings = {}
        for row in rows:
            try:
                payload = json.loads(row['binding_json'])
                if not isinstance(payload, dict):
                    raise TypeError
                bindings[(row['source_hash'], row['turn'])] = payload
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                _skip('binding', exc)
        self.bindings = bindings


def fallback(service, args):
    args = args or {}
    if not isinstance(args, dict):
        return {'status': 'failed', 'code': 'invalid_briefing_context'}
    sid = args.get('session_id')
    if not isinstance(sid, str) or not sid.strip():
        return {'status': 'failed', 'code': 'invalid_briefing_context'}
    try:
        mid = int(args.get('keep_from_message_id') or 0)
        ids = args.get('context_message_ids')
        if isinstance(args.get('keep_from_message_id'), bool) or mid < 0:
            raise ValueError()
        if ids is not None and (not isinstance(ids, list) or any(
                isinstance(i, bool) or not str(i).isdecimal() or int(i) <= 0 for i in ids)):
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        return {'status': 'failed', 'code': 'invalid_briefing_context'}

    snapshot = get(service)
    metadata = {'degraded': True, 'snapshot_revision': snapshot.get('revision'),
                'expanded_count': 0, 'unexpanded_count': 0, 'unexpanded': []}
    if snapshot.get('status') in ('empty', 'failed'):
        return dict(snapshot, **metadata)
    try:
        if snapshot.get('status') != 'ok' or not isinstance(snapshot.get('text'), str):
            raise ValueError()
        cutoff = instant(snapshot['refreshed_at'])
        if type(snapshot['revision']) is not int or snapshot['revision'] < 1:
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError):
        return dict(metadata, status='failed', code='briefing_snapshot_unreadable')

    now = datetime.now(timezone.utc)
    cfg = lifecycle_config()['briefing']
    horizon = now - timedelta(hours=cfg['recent_hours'])
    candidates = []
    for event in service.store.all_events():
        try:
            created = instant(event.ingest_time)
        except (ValueError, TypeError, AttributeError):
            created = None
        if event.kind == 'promise':
            if event.status != 'open' or not event.due:
                continue
            try:
                due = instant(event.due)
            except (ValueError, TypeError, AttributeError):
                continue
            if due > now or not (due > cutoff or (created and cutoff < created <= now)):
                continue
        elif (event.kind == 'relationship' or event.origin == 'observe' or not created
              or not (cutoff < created <= now and created >= horizon)):
            continue
        candidates.append((event, created))

    reason = None
    try:
        with ExitStack() as stack:
            state = ReadState(service, stack)
            blocked = {r[0] for r in state.rows('memory_lifecycle.sqlite3', 'eligibility',
                       "SELECT id FROM eligibility WHERE state!='active'")}
            aliases = dict(state.rows('memory_lifecycle.sqlite3', 'aliases', 'SELECT id,target FROM aliases'))
            blocked.update(aliases)
            pinned = set()
            for eid in service.pin_store.all():
                seen = set()
                while eid in aliases:
                    if eid in seen:
                        raise ValueError('memory_alias_cycle')
                    seen.add(eid)
                    eid = aliases[eid]
                pinned.add(eid)
            keep_turn = service.turn_ledger.turn_at_or_after(sid, mid) if mid else 0
            recent, promises = [], []
            for event, created in candidates:
                if event.event_id in blocked:
                    continue
                is_promise = event.kind == 'promise'
                if not is_promise and (event.event_id in pinned or not service.sleep_state.recall_allowed(event.event_id)):
                    continue
                if mid or ids is not None:
                    sources = state.sources(event)
                    if sources and all(s.get('assistant_message_id') is not None for s in sources):
                        onstage = sources_in_context(sources, sid, mid, ids)
                    else:
                        merged = state.rows('pin_policy.sqlite3', 'merged',
                            'SELECT source_ids FROM merged WHERE event_id=?', (event.event_id,))
                        onstage = bool(ids is None and not merged and keep_turn
                            and event.source_ref == f'conversation/session-{sid}'
                            and any(end >= keep_turn for _, end in service.turn_spans.span_of_event(event)))
                    if onstage:
                        continue
                if is_promise:
                    body = event.summary if event.event_id in pinned else event.core
                    line = f'约定到期 [{event.event_id}] {body}（due={event.due}，open）'
                    promises.append((event, line))
                else:
                    body = event.core if event.significance in (8, 9, 10) else event.summary
                    line = f'• [{service._briefing_when(event)}] {body}'
                    recent.append((event, line, created))
            recent.sort(key=lambda row: (-row[0].significance, -row[2].timestamp(), row[0].event_id))
            additions = [(event, line) for event, line, _ in recent] + promises
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        # State cannot safely establish eligibility or provenance; keep the saved text usable.
        additions = [(event, '') for event, _ in candidates]
        reason = 'local_state_unavailable'

    text = snapshot['text']
    visible_ids = snapshot.get('visible_event_ids')
    if isinstance(visible_ids, list):
        metadata['visible_event_ids'] = list(visible_ids)
    directory = snapshot.get('entity_directory', '')
    if not isinstance(directory, str) or not directory or not text.endswith(directory):
        directory = ''
    if directory:
        text = text.removesuffix(directory).removesuffix('\n')
    if additions:
        expanded = '\n'.join(part for part in [text, NOTICE, *(line for _, line in additions)] if part)
        if reason is None and len(expanded) <= cfg['total_chars']:
            text = expanded
            metadata['expanded_count'] = len(additions)
            if isinstance(visible_ids, list):
                metadata['visible_event_ids'] = list(dict.fromkeys(
                    visible_ids + [event.event_id for event, _ in additions]))
        else:
            reason = reason or 'total_chars_exceeded'
            metadata['unexpanded'] = [{'event_id': event.event_id, 'reason': reason} for event, _ in additions]
            metadata['unexpanded_count'] = len(additions)
            event_ids = '、'.join(event.event_id for event, _ in additions)
            text += (f'\n{NOTICE}\n有 {len(additions)} 条新记忆没有展开；Event ID：{event_ids}。'
                     '请用 search_memory / read_memory_source 查看。')
    text = '\n'.join(part for part in (text, directory) if part)
    return dict(metadata, status='ok', text=text, chars=len(text), session_id=sid,
                revision=snapshot['revision'], refreshed_at=snapshot['refreshed_at'])
