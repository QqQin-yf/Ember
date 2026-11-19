import os
import tempfile
import unittest
from unittest.mock import patch

from ember.extraction import Extraction
from ember.knowledge import KnowledgeStore
from ember.llm import MockLLMClient
from ember.models import Event
from ember.service import EmberService
from ember.slice_embedding import SliceEmbeddingConfig
from ember.store import EmberStore
from ember.tool_catalog import visible_specs
from ember.turn_slices import TurnSliceStore


class ToolSlimmingTests(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.service = EmberService(EmberStore(root), MockLLMClient())
        self.slices = TurnSliceStore(root)
        self.knowledge = KnowledgeStore(root, 'default')
        self.enterContext(patch.dict(os.environ, {'EMBER_PRIVATE_DELAY_SECONDS': '0'}))
        self.config = SliceEmbeddingConfig('fixture-only')
        self.enterContext(patch('ember.extraction.SliceEmbeddingConfig.load', return_value=self.config))

    def add_source(self, seq, text):
        source = self.slices.save('main', [
            {'message_id': seq, 'role': 'user', 'content': text, 'created_at': '2026-11-05T10:00:00+08:00'},
            {'message_id': seq + 1, 'role': 'assistant', 'content': 'Supporting answer long enough to meet the extraction minimum.',
             'created_at': 1793844000000}])
        self.slices.save_embedding(source, self.config.space, [1., 0.])
        return source

    def test_scope_and_downstream_pins(self):
        normal = {s['name'] for s in visible_specs(self.service, {})}
        self.assertNotIn('get_memory_extraction_batch', normal)
        self.assertNotIn('finish_memory_extraction_group', normal)
        self.assertNotIn('read_memory_slices', normal)
        self.assertNotIn('pin_memory', normal)
        self.assertIn('set_memory_pin', normal)
        self.service._generation_contexts['extraction-fixture'] = {'extraction_scope': {}}
        maintenance = {s['name'] for s in visible_specs(self.service, {'recall_id': 'extraction-fixture'})}
        self.assertTrue({'get_memory_extraction_batch', 'finish_memory_extraction_group', 'review_memory_knowledge'} <= maintenance)
        first = self.add_source(1, 'Original discussion.')
        mid = self.knowledge.create('pattern', 'A workflow', ['original'], [first['slice_id']])['memory_id']
        for pinned in (False, True):
            result = self.service.sidecar_call_tool('set_memory_pin', {'kind': 'knowledge', 'id': mid, 'pinned': pinned})
            self.assertIs(result['pinned'], pinned)
        result = self.service.sidecar_call_tool('read_memory_source', {'knowledge_id': mid})
        self.assertEqual(result['slices'][0]['messages'][0]['content'], 'Original discussion.')

    def test_search_returns_quotes_from_outside_the_window(self):
        first = self.add_source(1, 'First decision. A second decision.')
        self.add_source(3, 'A decision already in context.')
        self.add_source(5, 'Future decision outside this branch.')
        active = {'session': self.service.get_or_create_session('main'),
                  'request_context': {'user_message_id': 5, 'context_message_ids': [3, 4]}}
        with patch('ember.slice_recall.SliceEmbeddingClient.embed') as embed:
            out = self.service.sidecar_search_memory({'query': 'decision', 'scope': 'source'}, active)
            embed.assert_not_called()
        self.assertEqual(out['total'], 1, out)
        self.assertEqual(out['items'][0]['slice_id'], first['slice_id'])
        self.assertIn('decision', out['items'][0]['quote'])
        self.assertEqual(set(out['items'][0]), {'slice_id', 'date', 'quote'})

    def test_source_phrase_precedes_vectors_and_words_have_boundaries(self):
        exact = self.add_source(1, 'A cat, on the mat.')
        self.add_source(3, 'Concatenate the results and check the floor.')
        with patch('ember.slice_recall.SliceEmbeddingClient.embed', return_value=[1., 0.]) as embed:
            for query in ('cat', 'cat on the mat'):
                result = self.service.sidecar_search_memory({'query': query, 'scope': 'source'})
                self.assertEqual(result['total'], 1, result)
                self.assertEqual(result['items'][0]['slice_id'], exact['slice_id'])
            embed.assert_not_called()
            result = self.service.sidecar_search_memory({'query': 'feline companion', 'scope': 'source'})
            embed.assert_called_once()
            self.assertGreater(result['total'], 0, result)

    def test_scoped_event_search_includes_knowledge_and_omits_visible_items(self):
        source = self.add_source(1, 'The orchard decision used the southern plot.')
        mid = self.knowledge.create('pattern', 'An orchard decision', ['orchard'], [source['slice_id']])['memory_id']
        events = ['EVT-100', 'EVT-200']
        for event_id in events:
            self.service.store.write_event(Event(event_id=event_id, version_id=f'{event_id}-v1',
                source_ref='conversation/session-main/turn-1', raw_ref='conversation/session-main/turn-1',
                event_time='2026-11-05T10:00:00+08:00', ingest_time='2026-11-05T10:00:00+08:00',
                core='Orchard decision', summary='Orchard decision'))
        window = {'request_context': {'visible_event_ids': [events[0]]}}
        with patch('ember.service.embed_text', return_value=None), patch.object(
                self.service, 'read_source_event', return_value={'messages': source['messages']}):
            result = self.service.sidecar_search_memory({'query': 'orchard', 'scope': 'event'}, window)
        self.assertEqual(result['total'], 2, result)
        self.assertEqual({i.get('event_id') or i['knowledge_id'] for i in result['items']}, {events[1], mid})
        self.assertEqual({i['quote'] for i in result['items']}, {'The orchard decision used the southern plot.'})
        self.assertTrue(all(i['date'] for i in result['items']))

    def test_review_failure_keeps_current_group_and_success_advances(self):
        old = self.add_source(99, 'The old decision.')
        mid = self.knowledge.create('pattern', 'A decision habit', ['decision'], [old['slice_id']])['memory_id']
        first = self.add_source(1, 'First decision.')
        second = self.add_source(3, 'Second decision.')
        queue = Extraction(self.service)
        with patch('ember.extraction.ask', side_effect=lambda purpose, instruction, material, schema, **kwargs:
                {'groups': [{'description': s['slice_id'], 'slice_ids': [s['slice_id']]} for s in material['slices']]}):
            queue.discover(self.config)
        active = {'extraction_scope': {'session_id': 'main', 'assistant_message_ids': [2,4],
                                       'slice_ids': [first['slice_id'],second['slice_id']]}}
        call = lambda args: self.service._dispatch_memory_tool('review_memory_knowledge', args, active)
        started = call({})
        self.assertFalse(started['done'], started)
        self.assertNotIn('group_id', started['material'])
        rejected = call({'items': [{'memory_id': mid, 'action': 'keep', 'confidence': 4}]})
        self.assertEqual(set(rejected), {'status', 'code', 'error', 'hint'})
        self.assertEqual(self.knowledge.all()[0]['confidence'], 8)
        self.assertEqual(call({}), started)
        kept = {'items': [{'memory_id': mid, 'action': 'keep', 'confidence': 7}]}
        with queue.db() as db:
            db.execute("CREATE TRIGGER reject_review BEFORE INSERT ON reviewed_groups "
                       "BEGIN SELECT RAISE(ABORT, 'test rollback'); END")
        failed_write = call(kept)
        self.assertEqual(failed_write['error'], 'review_storage_error', failed_write)
        self.assertEqual(self.knowledge.all()[0]['confidence'], 8)
        with queue.db() as db:
            db.execute('DROP TRIGGER reject_review')
        self.assertEqual(call({}), started)
        advanced = call(kept)
        self.assertFalse(advanced['done'], advanced)
        self.assertNotEqual(advanced['material'], started['material'])
        self.assertEqual(self.knowledge.all()[0]['confidence'], 7)
        self.assertEqual(call(kept)['done'], True)
        with queue.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM reviewed_groups').fetchone()[0], 2)
