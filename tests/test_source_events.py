"""by-source lookup: a busy store and one bad record stay readable failures."""
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ember.knowledge import KnowledgeStore
from ember.llm import MockLLMClient
from ember.service import EmberService
from ember.source_events import (
    database_busy, events_for_sources, failure_from_exception, response_for)
from ember.store import EmberStore
from ember.turn_slices import TurnSliceStore
from tests.test_sidecar import write_args


class SourceEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.fetch = patch.object(self.service, '_fetched_corpus', return_value=[])
        self.fetch.start()
        self.addCleanup(self.fetch.stop)
        self.service.hook_turn_start(
            'chat', '今天我们决定把传播模型改成 Route Search。',
            user_message_id=71, recall_id='r')

    def write(self):
        result = self.service.sidecar_call_tool(
            'write_event', write_args(), recall_id='r', tool_call_id='t')
        self.assertEqual(result['status'], 'created', result)
        return result

    def test_lists_event_for_its_session_and_message(self):
        result = self.write()
        found = events_for_sources(
            self.service, {'session_id': 'chat', 'message_ids': [71, '71']})
        self.assertEqual(found, {'status': 'ok', 'events': [
            {'event_id': result['event_id'], 'title': result['summary']}]})
        self.assertEqual(
            events_for_sources(self.service, {'session_id': 'other', 'message_ids': [71]})['events'],
            [])

    def test_skips_one_event_with_incomplete_provenance(self):
        first = self.write()
        second = self.service.sidecar_call_tool(
            'write_event', write_args(), recall_id='r', tool_call_id='t2')
        self.assertEqual(second['status'], 'created', second)
        self.service.event_sources._records[second['event_id']]['turn_messages'] = None
        found = events_for_sources(self.service, {'session_id': 'chat', 'message_ids': [71]})
        self.assertEqual(found, {'status': 'ok', 'events': [
            {'event_id': first['event_id'], 'title': first['summary']}]})

    def test_skips_one_slice_ref_missing_its_id(self):
        result = self.write()
        saved = TurnSliceStore(self.tmp.name).save('chat', [
            {'message_id': 81, 'role': 'user', 'content': 'source'},
            {'message_id': 82, 'role': 'assistant', 'content': 'reply'}])
        store = KnowledgeStore(self.tmp.name, 'default')
        good = store.sources([saved['slice_id']])[0]
        broken = {'content_hash': good['content_hash'], 'session_id': 'chat',
                  'user_message_id': '81', 'assistant_message_id': '82'}
        store.save_event_sources(result['event_id'], [broken, good])
        found = events_for_sources(self.service, {'session_id': 'chat', 'message_ids': [81]})
        self.assertEqual(found, {'status': 'ok', 'events': [
            {'event_id': result['event_id'], 'title': result['summary']}]})

    def test_locked_store_returns_store_busy_without_the_sqlite_text(self):
        self.write()
        path = Path(self.tmp.name, 'memory_lifecycle.sqlite3')
        conn = sqlite3.connect(path)
        conn.execute('BEGIN EXCLUSIVE')
        try:
            started = time.perf_counter()
            found = events_for_sources(self.service, {'session_id': 'chat', 'message_ids': [71]})
            elapsed = time.perf_counter() - started
        finally:
            conn.rollback()
            conn.close()
        self.assertEqual(found, {'status': 'failed', 'code': 'store_busy'})
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 2)

    def test_unreadable_alias_row_fails_the_request(self):
        class Bad:
            def __getitem__(self, index):
                raise IndexError('row')

        def rows(state, name, table, sql, params=()):
            return [Bad()]

        self.write()
        with patch('ember.source_events.ReadState.rows', rows):
            found = events_for_sources(self.service, {'session_id': 'chat', 'message_ids': [71]})
        self.assertEqual(found, {'status': 'failed', 'code': 'source_record_unreadable'})

    def test_failure_body_keeps_the_code_and_drops_the_exception_text(self):
        secret = RuntimeError(r'D:\secret\memory text')
        with self.assertLogs('ember.source_events', level='WARNING') as caught:
            body, status = failure_from_exception(secret)
        self.assertEqual(body, {'status': 'failed', 'code': 'internal_error'})
        self.assertEqual(status, 500)
        logged = '\n'.join(caught.output)
        self.assertIn('RuntimeError', logged)
        self.assertNotIn('secret', logged)
        self.assertNotIn('memory text', logged)

        column = sqlite3.OperationalError('no such column: content near secret')
        self.assertFalse(database_busy(column))
        with self.assertLogs('ember.source_events', level='WARNING') as caught:
            body, status = failure_from_exception(column)
        self.assertEqual((body, status), ({'status': 'failed', 'code': 'internal_error'}, 500))
        self.assertNotIn('secret', '\n'.join(caught.output))
        self.assertNotIn('column', '\n'.join(caught.output))

        payload, status = response_for({'status': 'failed', 'code': 'store_busy', 'error': 'database is locked'})
        self.assertEqual((payload, status), ({'status': 'failed', 'code': 'store_busy'}, 503))
        payload, status = response_for({'status': 'failed', 'code': 'invalid_source_selection'})
        self.assertEqual(status, 400)
        payload, status = response_for({'status': 'failed', 'code': 'source_record_unreadable'})
        self.assertEqual(status, 500)
        payload, status = response_for({'status': 'ok', 'events': []})
        self.assertEqual((payload, status), ({'status': 'ok', 'events': []}, 200))


if __name__ == '__main__':
    unittest.main()
