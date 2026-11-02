"""Fallback keeps a frozen prefix and never joins or mutates refresh work."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

from ember.memory_lifecycle import Lifecycle
from ember.knowledge import KnowledgeStore
from ember.turn_slices import TurnSliceStore
from tests import test_briefing_v2 as fixtures


class BriefingFallbackTests(unittest.TestCase):
    setUp = fixtures.FiveLayerBriefingTests.setUp
    event = fixtures.FiveLayerBriefingTests.event
    knowledge = fixtures.FiveLayerBriefingTests.knowledge
    refresh = fixtures.FiveLayerBriefingTests.refresh

    def fallback(self, **kwargs):
        with patch('ember.briefing_fallback.lifecycle_config', return_value=self.cfg):
            return self.service.sidecar_briefing_fallback(dict(session_id='current', **kwargs))

    def files(self):
        return {str(p.relative_to(self.tmp.name)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in Path(self.tmp.name).rglob('*') if p.is_file()}

    def test_snapshot_prefix_layers_scores_and_no_writes(self):
        self.event('existing');self.knowledge('OLD-KNOWLEDGE')
        snapshot = self.refresh()
        self.event('new-summary', score=5)
        self.event('new-core', score=9)
        self.event('new-ten', score=10)
        self.event('new-due', 'promise', due=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
        self.event('new-pin', pin=True)
        self.event('new-relation', 'relationship')
        self.knowledge('NEW-KNOWLEDGE')
        self.event('future', 'promise', due=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat())
        self.event('without-due', 'promise')
        before = self.files()
        report = self.fallback()
        self.assertEqual(self.files(), before)
        self.assertTrue(report['text'].startswith(snapshot['text']+'\n以下为'))
        self.assertEqual(report['snapshot_revision'], snapshot['revision'])
        self.assertEqual(report['expanded_count'], 4)
        self.assertEqual(report['unexpanded_count'], 0)
        text = report['text']
        for value in ('CORE-new-core', 'CORE-new-ten', 'SUMMARY-new-summary', 'CORE-new-due'):
            self.assertIn(value, text)
        self.assertLess(text.index('CORE-new-ten'), text.index('CORE-new-core'))
        self.assertLess(text.index('SUMMARY-new-summary'), text.index('CORE-new-due'))
        for value in ('CORE-new-pin', 'RAW-new-pin', 'CORE-new-relation', 'NEW-KNOWLEDGE', 'CORE-future', 'CORE-without-due'):
            self.assertNotIn(value, text)
        self.assertEqual(self.service.sidecar_briefing_get(), snapshot)

    def test_existing_promise_newly_due_and_old_overdue_not_repeated(self):
        now = datetime.now(timezone.utc)
        self.event('already-due', 'promise', due=(now-timedelta(hours=1)).isoformat())
        self.event('becomes-due', 'promise', due=(now+timedelta(hours=1)).isoformat())
        self.refresh()
        with patch('ember.briefing_fallback.datetime') as clock:
            clock.now.return_value = now+timedelta(hours=2)
            report = self.fallback()
        self.assertEqual(report['expanded_count'], 1)
        self.assertEqual(report['text'].count('CORE-already-due'), 1)
        self.assertIn('CORE-becomes-due', report['text'])

    def test_recent_horizon_completion_and_recall_exclusions(self):
        self.refresh()
        for eid in ('old-ten', 'sleep-blocked', 'life-blocked', 'observe', 'active'):
            event = self.event(eid, score=10)
            if eid == 'observe':
                event.origin = 'observe'
        self.service.sleep_state.mark('sleep-blocked', 'outdated')
        Lifecycle(self.tmp.name).mark('life-blocked', 'no_recall', 'test', ['test'])
        event = self.event('done', 'promise', due=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
        event.status = 'done'
        # Only old-ten ages out; the remaining additions are written close to the later read.
        now = datetime.now(timezone.utc)+timedelta(hours=73)
        for event in self.service.store.all_events():
            if event.event_id != 'old-ten':
                event.ingest_time = (now-timedelta(minutes=1)).isoformat()
        with patch('ember.briefing_fallback.datetime') as clock:
            clock.now.return_value = now
            report = self.fallback()
        self.assertEqual(report['expanded_count'], 1)
        self.assertIn('CORE-active', report['text'])

    def test_budget_never_partially_expands_or_truncates_a_core(self):
        snapshot = self.refresh()
        event = self.event('large', score=9);event.core = '完整记忆'*100
        self.event('small', score=2)
        self.cfg['briefing']['total_chars'] = len(snapshot['text'])+100
        report = self.fallback()
        self.assertEqual(report['expanded_count'], 0)
        self.assertEqual(report['unexpanded_count'], 2)
        self.assertEqual({r['event_id'] for r in report['unexpanded']}, {'large', 'small'})
        self.assertEqual({r['reason'] for r in report['unexpanded']}, {'total_chars_exceeded'})
        self.assertNotIn('完整记忆', report['text'])
        self.assertNotIn('SUMMARY-small', report['text'])
        self.assertIn('search_memory / read_memory_source', report['text'])

    def test_exact_budget_boundary_includes_header_and_newlines(self):
        self.refresh();self.event('fits')
        report = self.fallback()
        self.cfg['briefing']['total_chars'] = report['chars']
        self.assertEqual(self.fallback()['expanded_count'], 1)
        self.cfg['briefing']['total_chars'] -= 1
        self.assertEqual(self.fallback()['unexpanded_count'], 1)

    def test_empty_invalid_and_corrupt_snapshots(self):
        self.assertEqual(self.fallback()['status'], 'empty')
        for args in ({}, {'session_id': ' '}, {'session_id': 's', 'context_message_ids': [True]},
                     {'session_id': 's', 'keep_from_message_id': -1}):
            self.assertEqual(self.service.sidecar_briefing_fallback(args)['code'], 'invalid_briefing_context')
        for value in ('[]', '{', '{}', '{"status":"ok","revision":1,"text":"x"}'):
            Path(self.service._briefing_path()).write_text(value, encoding='utf-8')
            self.assertEqual(self.fallback()['code'], 'briefing_snapshot_unreadable')

    def test_no_additions_returns_exact_snapshot_text_and_no_database_initialization(self):
        path = Path(self.service._briefing_path())
        path.write_text(json.dumps({'status': 'ok', 'text': '快照原文\n', 'revision': 7,
            'refreshed_at': datetime.now(timezone.utc).isoformat()}), encoding='utf-8')
        before = self.files()
        report = self.fallback()
        self.assertEqual(report['text'], '快照原文\n')
        self.assertTrue(report['degraded'])
        self.assertEqual(report['expanded_count'], 0)
        self.assertEqual(self.files(), before)

    def test_context_filters_only_all_sources_present(self):
        self.refresh()
        knowledge = KnowledgeStore(self.tmp.name)
        refs = []
        for session, mid in [('current', 1), ('other', 3)]:
            source = TurnSliceStore(self.tmp.name).save(session, [
                {'message_id': mid, 'role': 'user', 'content': 'source'},
                {'message_id': mid+1, 'role': 'assistant', 'content': 'reply'}])
            refs.extend(knowledge.sources([source['slice_id']]))
        self.event('present');self.event('disjoint');self.event('unknown')
        knowledge.save_event_sources('present', refs[:1])
        knowledge.save_event_sources('disjoint', refs)
        before = self.files()
        for args in ({'context_message_ids': ['1', '2']}, {'keep_from_message_id': 1}):
            report = self.fallback(**args)
            self.assertNotIn('CORE-present', report['text'])
            self.assertIn('CORE-disjoint', report['text'])
            self.assertIn('CORE-unknown', report['text'])
        self.assertEqual(self.files(), before)

    def test_refresh_lock_and_model_sync_timeline_work_are_not_joined(self):
        self.refresh();self.event('new')
        release, acquired = threading.Event(), threading.Event()
        def hold_refresh():
            with self.service._briefing_lock:
                acquired.set()
                release.wait(5)
        with ThreadPoolExecutor(max_workers=2) as pool:
            worker = pool.submit(hold_refresh)
            self.assertTrue(acquired.wait(2))
            try:
                with patch('ember.briefing.sync_increment', side_effect=AssertionError('sync')), \
                     patch('ember.timeline.Timeline.render', side_effect=AssertionError('timeline')):
                    report = pool.submit(self.fallback).result(timeout=2)
                self.assertEqual(report['expanded_count'], 1)
            finally:
                release.set()
                worker.result(timeout=2)

    def test_frozen_and_completed_source_identities_use_only_read_only_queries(self):
        from ember.generations import fingerprint
        self.refresh()
        event = self.event('frozen')
        self.service.event_sources.capture('frozen', event.version_id, 'current', [(1, 1)], [
            {'turn': 1, 'role': 'user', '_source_session_id': 'current', '_source_message_id': 11}],
            writer_recall_id='writer', writer_turn=1)
        report = self.fallback(context_message_ids=[11, 12])
        self.assertIn('CORE-frozen', report['text'])
        source = self.service.event_sources.get('frozen')
        binding = {'turn': 1, 'user_message_id': '11', 'assistant_message_id': '12',
                   'recall_id': 'writer', 'status': 'done', 'evidence': 'turn_end'}
        with self.service.generations._db() as conn:
            conn.execute('INSERT INTO source_completions VALUES (?,?,?)',
                         (fingerprint(source), 1, json.dumps(binding)))
        before = self.files()
        self.assertNotIn('CORE-frozen', self.fallback(context_message_ids=[11, 12])['text'])
        self.assertIn('CORE-frozen', self.fallback(context_message_ids=[11])['text'])
        self.assertEqual(self.files(), before)

    def test_merged_sources_and_changed_slice_are_not_mistaken_for_present(self):
        self.refresh()
        from ember.pin_policy import Pins
        store = KnowledgeStore(self.tmp.name)
        refs = []
        for sid, mid in [('current', 1), ('other', 3)]:
            source = TurnSliceStore(self.tmp.name).save(sid, [
                {'message_id': mid, 'role': 'user', 'content': 'source'},
                {'message_id': mid+1, 'role': 'assistant', 'content': 'reply'}])
            refs.append(store.sources([source['slice_id']]))
        self.event('member-a');self.event('member-b');self.event('merged');self.event('changed')
        store.save_event_sources('member-a', refs[0]);store.save_event_sources('member-b', refs[1])
        store.save_event_sources('changed', [dict(refs[0][0], content_hash='outdated')])
        with Pins(self.service).db() as conn:
            conn.execute('INSERT INTO merged VALUES (?,?)', ('merged', json.dumps(['member-a', 'member-b'])))
        text = self.fallback(context_message_ids=[1, 2])['text']
        self.assertNotIn('CORE-member-a', text)
        self.assertIn('CORE-merged', text)
        self.assertIn('CORE-changed', text)

    def test_locked_local_state_returns_snapshot_and_ids_immediately(self):
        snapshot = self.refresh();self.event('new')
        conn = sqlite3.connect(Path(self.tmp.name, 'memory_lifecycle.sqlite3'))
        try:
            conn.execute('BEGIN EXCLUSIVE')
            with ThreadPoolExecutor(max_workers=1) as pool:
                report = pool.submit(self.fallback).result(timeout=2)
            self.assertTrue(report['text'].startswith(snapshot['text']))
            self.assertEqual(report['unexpanded'], [{'event_id': 'new', 'reason': 'local_state_unavailable'}])
            self.assertEqual(report['expanded_count'], 0)
        finally:
            conn.rollback();conn.close()


if __name__ == '__main__':
    unittest.main()
