import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ember.slice_embedding import SliceEmbeddingConfig, SliceEmbeddingClient, SliceEmbeddingError
from ember.slice_recall import SliceRecall
from ember.turn_slices import TurnSliceStore
from ember.service import EmberService
from ember.store import EmberStore
from ember.llm import MockLLMClient
from ember.models import Event, RecallRoute


class SliceRecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = SliceEmbeddingConfig('synthetic-secret')
        patcher = patch.object(SliceEmbeddingConfig, 'load', return_value=self.config)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.store = TurnSliceStore(self.tmp.name)
        self.recall = SliceRecall(self.tmp.name)

    def add(self, seq, vector, session='old', instance='default'):
        source = self.store.save(session, [
            {'message_id': seq, 'role': 'user', 'content': 'original user ' + str(seq)},
            {'message_id': seq + 1, 'role': 'assistant', 'content': 'original reply ' + str(seq)}],
            instance=instance)
        self.store.save_embedding(source, self.config.space, vector)
        return source

    def match(self, text='query', **kwargs):
        with patch.object(SliceEmbeddingClient, 'embed', return_value=[1., 0.]) as embed:
            metadata = self.recall.match(text, **kwargs)
        return metadata, embed

    def test_whole_input_is_embedded_once_and_complete_ranking_is_saved(self):
        first = self.add(1, [1., 0.])
        middle = self.add(3, [0., 1.])
        last = self.add(5, [-1., 0.])
        metadata, embed = self.match('完整输入。第二句话？')
        embed.assert_called_once_with('完整输入。第二句话？')
        trace = SliceRecall(self.tmp.name).trace(metadata['trace_id'])
        self.assertEqual([r['slice_id'] for r in trace['rankings']],
                         [first['slice_id'], middle['slice_id'], last['slice_id']])
        self.assertEqual([r['score'] for r in trace['rankings']], [1., 0., -1.])
        self.assertIsNone(trace['candidate_threshold'])
        self.assertFalse(trace['injected'])
        self.assertNotIn('original user', json.dumps(trace))
        self.assertNotIn('synthetic-secret', Path(self.recall.path).read_bytes().decode(errors='ignore'))

    def test_each_new_input_gets_a_fresh_vector(self):
        self.add(1, [1., 0.])
        with patch.object(SliceEmbeddingClient, 'embed', return_value=[1., 0.]) as embed:
            a = self.recall.match('same input')
            b = self.recall.match('same input')
        self.assertEqual(embed.call_count, 2)
        self.assertNotEqual(a['trace_id'], b['trace_id'])

    def test_stale_other_model_and_other_instance_vectors_do_not_mix(self):
        stale = self.add(1, [1., 0.])
        self.store.save('old', [
            {'message_id': 1, 'role': 'user', 'content': 'edited original'},
            {'message_id': 2, 'role': 'assistant', 'content': 'changed reply'}])
        wrong = self.add(3, [1., 0.], instance='other')
        with self.store._db() as db:
            db.execute('UPDATE slice_embeddings SET space=? WHERE slice_id=?',
                       ('different-model-space', stale['slice_id']))
        result, embed = self.match()
        self.assertEqual(result['status'], 'no_vectors')
        embed.assert_not_called()

    def test_dimension_mismatch_is_rejected_instead_of_partial_dot_product(self):
        self.add(1, [1., 0., 1.])
        result, _ = self.match()
        self.assertEqual(result['ranked_count'], 0)
        self.assertEqual(result['invalid_vectors'], 1)

    def test_window_and_current_or_future_messages_are_excluded(self):
        self.add(1, [1., 0.], session='s')
        self.add(3, [1., 0.], session='s')
        self.add(5, [1., 0.], session='s')
        result, _ = self.match(session_id='s', anchor_message_id=5, context_start_id=3)
        self.assertEqual(result['ranked_count'], 1)
        self.assertEqual(result['filtered_by_window'], 1)
        self.assertEqual(result['filtered_by_anchor'], 1)

    def test_trace_sources_are_only_read_if_content_fingerprint_still_matches(self):
        source = self.add(1, [1., 0.])
        result, _ = self.match()
        trace = self.recall.trace(result['trace_id'], include_sources=True)
        self.assertEqual(len(trace['rankings'][0]['messages']), 2)
        self.assertIsNone(SliceRecall(self.tmp.name, 'other').trace(result['trace_id']))
        self.store.save('old', [
            {'message_id': 1, 'role': 'user', 'content': 'changed'},
            {'message_id': 2, 'role': 'assistant', 'content': 'changed'}])
        trace = self.recall.trace(result['trace_id'], include_sources=True)
        self.assertEqual(trace['rankings'][0]['source_status'], 'changed_or_missing')
        self.assertNotIn('messages', trace['rankings'][0])

    def test_model_failure_is_durable_and_does_not_fall_back_to_old_routes(self):
        self.add(1, [1., 0.])
        with patch.object(SliceEmbeddingClient, 'embed', side_effect=SliceEmbeddingError('embedding_http_429')):
            result = self.recall.match('query')
        trace = self.recall.trace(result['trace_id'])
        self.assertEqual(trace['status'], 'unavailable')
        self.assertEqual(trace['error_code'], 'embedding_http_429')
        self.assertEqual(trace['rankings'], [])

    def test_automatic_hook_retires_all_old_routes_and_freezes_diagnostic_on_retry(self):
        self.add(1, [1., 0.])
        svc = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        svc.store.write_event(Event(event_id='old', version_id='v', source_ref='x', raw_ref='x',
            event_time='2026-01-01', ingest_time='2026-01-01', summary='beacon', core='old body'))
        svc.store.write_route(RecallRoute(route_id='cue', event_id='old', matcher='lexical',
                                         cue_type='lexical', cue='beacon'))
        with patch('ember.service.DirectRecall', side_effect=AssertionError('retired automatic route')), \
                patch('ember.service.EntityRecall', side_effect=AssertionError('retired automatic route')), \
                patch.object(svc, '_observe_extra_hit', side_effect=AssertionError('retired automatic route')), \
                patch.object(SliceEmbeddingClient, 'embed', return_value=[1., 0.]) as embed:
            first = svc.hook_turn_start('s', 'beacon', user_message_id=100, recall_id='rid')
            retry = svc.hook_turn_start('s', 'beacon', user_message_id=100, recall_id='rid')
        self.assertEqual(first, retry)
        self.assertEqual(embed.call_count, 1)
        self.assertFalse(first['injected'])
        self.assertIsNone(first['injected_text'])
        self.assertEqual(first['recall_refs']['events'], [])
        self.assertEqual(first['slice_recall']['ranked_count'], 1)
        self.assertEqual(svc.recall_ledger.event_recall_count, {})
        snapshot = svc.sidecar_recall_snapshot('rid')['snapshot']
        self.assertEqual(snapshot['slice_recall'], first['slice_recall'])

    def test_active_search_memory_still_uses_event_content(self):
        svc = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        svc.store.write_event(Event(event_id='old', version_id='v', source_ref='x', raw_ref='x',
            event_time='2026-01-01', ingest_time='2026-01-01', summary='beacon', core='old body'))
        with patch('ember.service.embed_text', side_effect=RuntimeError('offline')):
            result = svc.sidecar_search_memory({'query': 'beacon'})
        self.assertIn('old', json.dumps(result))
