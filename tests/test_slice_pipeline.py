import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock

from ember import source_client
from ember.instances import InstanceRegistry
from ember.slice_embedding import SliceEmbeddingClient, SliceEmbeddingError, SliceEmbeddingConfig
from ember.slice_pipeline import SlicePipeline, IngestionJobs, complete_turn, complete_run
from ember.turn_slices import TurnSliceStore


class SlicePipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = InstanceRegistry(self.tmp.name, ['default', 'other'])
        self.service = self.registry.service('default')
        self.root = Path(self.service.store.root)
        self.config = Path(self.tmp.name) / 'embedding.json'
        self.config.write_text(json.dumps({'api_key': 'synthetic-secret'}), encoding='utf-8')
        self.pipeline = SlicePipeline(self.registry, self.config)
        self.rows = [{'message_id': 1, 'role': 'user', 'content': 'provider original user'},
                     {'message_id': 2, 'role': 'assistant', 'content': 'provider original reply'}]

    def enqueue(self, system=False):
        return self.pipeline.enqueue(self.service, 's', 1, 2, system)

    def jobs(self):
        return IngestionJobs(self.root)

    def process(self):
        with patch.object(source_client, 'fetch_source_path', return_value=self.rows) as source, \
                patch.object(SliceEmbeddingClient, 'embed', return_value=[1., .5]) as embed:
            self.assertTrue(self.pipeline.process_one())
        return source, embed

    def retry_now(self, receipt):
        with self.jobs().db() as db:
            db.execute('UPDATE jobs SET retry_at=0 WHERE job_id=?', (receipt['job_id'],))

    def test_durable_queue_reopens_and_deduplicates(self):
        one = self.enqueue()
        self.assertEqual(one, self.enqueue())
        self.assertEqual(self.jobs().status(one['job_id'])['state'], 'pending')
        self.process()
        ready = self.jobs().status(one['job_id'])
        self.assertEqual(ready['state'], 'ready')
        self.assertEqual(self.enqueue()['status'], 'ready')
        self.assertFalse(self.pipeline.process_one())

    def test_full_pipeline_fetches_original_then_saves_slice_and_vector(self):
        receipt = self.enqueue()
        source, embed = self.process()
        source.assert_called_once_with('s', ['1', '2'], instance='default')
        embed.assert_called_once_with('user:\nprovider original user\n\nassistant:\nprovider original reply')
        job = self.jobs().status(receipt['job_id'])
        store = TurnSliceStore(str(self.root))
        self.assertEqual(len(store.get(job['slice_id'])['messages']), 2)
        config = SliceEmbeddingConfig.load(self.config)
        self.assertIsNotNone(store.embedding(job['slice_id'], config.space))

    def test_system_side_excluded_before_embedding(self):
        receipt = self.enqueue(system=True)
        _, embed = self.process()
        embed.assert_called_once_with('assistant:\nprovider original reply')
        job = self.jobs().status(receipt['job_id'])
        self.assertEqual(len(TurnSliceStore(str(self.root)).get(job['slice_id'])['messages']), 1)

    def test_assistant_only_run_uses_no_historical_user(self):
        receipt = self.pipeline.enqueue(self.service, 's', None, 2)
        with patch.object(source_client, 'fetch_source_path', return_value=self.rows[1:]) as fetch, \
                patch.object(SliceEmbeddingClient, 'embed', return_value=[1.]):
            self.pipeline.process_one()
        fetch.assert_called_once_with('s', ['2'], instance='default')
        self.assertEqual(self.jobs().status(receipt['job_id'])['state'], 'ready')

    def test_embedding_failure_keeps_slice_and_retries_without_refetch(self):
        receipt = self.enqueue()
        with patch.object(source_client, 'fetch_source_path', return_value=self.rows), \
                patch.object(SliceEmbeddingClient, 'embed', side_effect=SliceEmbeddingError('embedding_http_429')):
            self.pipeline.process_one()
        job = self.jobs().status(receipt['job_id'])
        self.assertEqual(job['state'], 'failed')
        self.assertIsNotNone(TurnSliceStore(str(self.root)).get(job['slice_id']))
        self.retry_now(receipt)
        with patch.object(source_client, 'fetch_source_path') as fetch, \
                patch.object(SliceEmbeddingClient, 'embed', return_value=[1.]):
            self.pipeline.process_one()
        fetch.assert_not_called()
        self.assertEqual(self.jobs().status(receipt['job_id'])['state'], 'ready')

    def test_missing_embedding_key_still_saves_raw_slice(self):
        self.config.write_text('{"api_key":""}', encoding='utf-8')
        receipt = self.enqueue()
        with patch.object(source_client, 'fetch_source_path', return_value=self.rows):
            self.pipeline.process_one()
        job = self.jobs().status(receipt['job_id'])
        self.assertEqual(job['error_code'], 'api_key_missing')
        self.assertIsNotNone(TurnSliceStore(str(self.root)).get(job['slice_id']))

    def test_source_failure_is_retained_for_retry(self):
        receipt = self.enqueue()
        with patch.object(source_client, 'fetch_source_path',
                side_effect=source_client.SourceUnavailable('secret provider body', 'source_unreachable')):
            self.pipeline.process_one()
        job = self.jobs().status(receipt['job_id'])
        self.assertEqual(job['error_code'], 'source_unreachable')
        self.assertIsNone(job['slice_id'])
        self.retry_now(receipt)
        self.process()
        self.assertEqual(self.jobs().status(receipt['job_id'])['state'], 'ready')

    def test_crashed_job_is_recovered_after_lease_expiry(self):
        receipt = self.enqueue()
        self.assertIsNotNone(self.jobs().claim())
        self.assertIsNone(self.jobs().claim())
        with self.jobs().db() as db:
            db.execute('UPDATE jobs SET lease_until=0')
        self.process()
        self.assertEqual(self.jobs().status(receipt['job_id'])['attempts'], 2)

    def test_worker_does_not_sweep_unscheduled_historical_slices(self):
        store = TurnSliceStore(str(self.root))
        old = store.save('old', self.rows)
        self.enqueue()
        self.process()
        self.assertIsNone(store.embedding(old['slice_id'], SliceEmbeddingConfig.load(self.config).space))

    def test_instances_do_not_share_jobs(self):
        other = self.registry.service('other')
        receipt = self.pipeline.enqueue(other, 's', 1, 2)
        self.process()
        self.assertIsNone(self.jobs().status(receipt['job_id']))
        self.assertEqual(IngestionJobs(other.store.root).status(receipt['job_id'])['state'], 'ready')

    def test_legacy_completion_enqueues_exact_start_identity_without_network(self):
        start = self.service.hook_turn_start('s', 'hook payload', user_message_id=1)
        with patch.object(source_client, 'fetch_source_path') as fetch:
            result = complete_turn(self.service, self.pipeline, 's', 'hook reply',
                                   start['turn'], assistant_message_id=2)
        fetch.assert_not_called()
        self.assertEqual(result['slice_ingestion']['status'], 'pending')
        self.process()

    def test_modern_completion_retry_after_service_restart_reuses_job(self):
        start = self.service.hook_turn_start('s', 'hook payload', user_message_id=1, recall_id='rid')
        result = complete_turn(self.service, self.pipeline, 's', 'hook reply',
            start['turn'], assistant_message_id=2, recall_id='rid')
        from ember.service import EmberService
        from ember.store import EmberStore
        from ember.llm import MockLLMClient
        reopened = EmberService(EmberStore(str(self.root)), MockLLMClient())
        retry = complete_turn(reopened, self.pipeline, 's', 'hook reply',
            start['turn'], assistant_message_id=2, recall_id='rid')
        self.assertEqual(result['slice_ingestion'], retry['slice_ingestion'])

    def test_rejected_cancelled_and_missing_identity_are_not_queued(self):
        rejected = complete_turn(self.service, self.pipeline, 's', 'no start', assistant_message_id=2)
        self.assertFalse(rejected['ok'])
        start = self.service.hook_turn_start('s', 'hook', user_message_id=1, recall_id='rid')
        cancelled = complete_turn(self.service, self.pipeline, 's', '', start['turn'],
                                  recall_id='rid', status='cancelled')
        self.assertNotIn('slice_ingestion', cancelled)
        self.assertFalse(self.jobs().path.exists())

    def test_completed_visible_run_enqueues_assistant_only(self):
        self.service.sidecar_run_finish = Mock(return_value={
            'session_id': 's', 'status': 'done', 'spoke': True, 'assistant_message_id': '2'})
        result = complete_run(self.service, self.pipeline, {})
        self.assertIn('slice_ingestion', result)
        for status, spoke in [('done', False), ('error', False), ('cancelled', False)]:
            self.service.sidecar_run_finish.return_value = {'status': status, 'spoke': spoke}
            self.assertNotIn('slice_ingestion', complete_run(self.service, self.pipeline, {}))
