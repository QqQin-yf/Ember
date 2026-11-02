"""Shared HTTP contracts for callers without Deliz's generation/window flow."""
from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from ember import source_client
from ember.instances import InstanceRegistry
from ember.knowledge import KnowledgeStore
from ember.models import Event, RecallRoute
from ember.turn_slices import TurnSliceStore


class CallerNeutralHTTPTests(unittest.TestCase):
    def setUp(self):
        import sidecar
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = InstanceRegistry(self.tmp.name, ['default', 'shared'])
        self.service = self.registry.service('shared')
        for patcher in (patch.object(sidecar, 'REGISTRY', self.registry),
                        patch.object(sidecar, 'TOKEN', ''),
                        patch.object(source_client, 'SOURCE_URL', ''),
                        patch.dict('os.environ', {'EMBER_MODEL_NETWORK_DISABLED': '1'})):
            patcher.start()
            self.addCleanup(patcher.stop)
        # No lifespan workers: HTTP routes run the real synchronous service work.
        self.client = TestClient(sidecar.app, headers={'X-Heart-Instance': 'shared'})
        self.addCleanup(self.client.close)
        now = datetime.now(timezone.utc).isoformat()
        self.service.store.write_event(Event('E', 'v1', 'source', 'source', now, now,
            summary='shared memory', core='remember the beacon', significance=8))
        self.service.store.write_route(RecallRoute('route-E', 'E', 'lexical', 'lexical', 'beacon'))

    def start(self, **fields):
        return self.client.post('/ember/hooks/turn_start', json={
            'session_id': 'cc-example', 'message': 'remember beacon', **fields})

    def test_minimal_recall_needs_no_message_ids_or_deliz_lifecycle(self):
        response = self.start()
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body['session_id'], 'cc-example')
        self.assertIn('remember the beacon', body['injected_text'])
        self.assertEqual(body['memory']['recalled_count'], 1)
        self.assertIsNone(body['recall_refs']['anchor_message_id'])
        self.assertEqual(self.start(session_id='example').json()['turn'], 1)
        self.assertEqual(self.start().json()['turn'], 2)

    def test_minimal_briefing_and_get_share_instance_without_source_configuration(self):
        response = self.client.post('/ember/briefing/refresh', json={'session_id': 'cc-example'})
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json()
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['session_id'], 'cc-example')
        self.assertIn('remember the beacon', report['text'])
        self.assertEqual(report['source_sync'], {'status': 'unavailable', 'code': 'source_unconfigured'})
        self.assertEqual(self.client.get('/ember/briefing').json(), report)
        self.assertEqual(self.client.get('/ember/briefing',
            headers={'X-Heart-Instance': 'default'}).json()['status'], 'empty')

    def test_source_outage_is_reported_without_rejecting_caller(self):
        with patch.object(source_client, 'SOURCE_URL', 'http://source.invalid'), \
                patch('ember.source_sync.SliceEmbeddingConfig.load'), \
                patch.object(source_client, 'fetch_source_increment',
                    side_effect=source_client.SourceUnavailable('offline', 'source_unreachable')):
            report = self.client.post('/ember/briefing/refresh',
                json={'session_id': 'cc-example'}).json()
        self.assertEqual(report['status'], 'ok')
        self.assertEqual(report['source_sync']['code'], 'source_unreachable')
        self.assertIn('remember the beacon', report['text'])

    def test_exact_window_filter_also_applies_without_recall_id(self):
        source = TurnSliceStore(self.service.store.root).save('cc-example', [
            {'message_id': 1, 'role': 'user', 'content': 'original'},
            {'message_id': 2, 'role': 'assistant', 'content': 'reply'}], instance='shared')
        knowledge = KnowledgeStore(self.service.store.root, 'shared')
        knowledge.save_event_sources('E', knowledge.sources([source['slice_id']]))
        body = self.start(context_message_ids=[1, 2]).json()
        self.assertFalse(body['injected'])
        self.assertEqual(body['memory']['recalled_count'], 0)
        self.assertEqual(self.service._live_turn['request_context']['context_message_ids'], [1, 2])

    def test_invalid_window_ids_fail_before_opening_a_turn(self):
        for value in ('1,2', [True], [-1]):
            response = self.start(context_message_ids=value)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()['code'], 'invalid_arguments')
        self.assertNotIn('cc-example', self.service.sessions)

    def test_bound_generation_accepts_cc_prefix_and_keeps_real_seq_requirement(self):
        rejected = self.start(recall_id='cc-generation')
        self.assertEqual(rejected.status_code, 400, rejected.text)
        first = self.start(recall_id='cc-generation', user_message_id=101)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()['session_id'], 'cc-example')
        self.assertEqual(self.start(recall_id='cc-generation', user_message_id=101).json(), first.json())


if __name__ == '__main__':
    unittest.main()
