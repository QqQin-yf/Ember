import unittest
from unittest.mock import patch
from tests import test_extraction as fixtures
from ember import extraction_pages as pages
from ember.extraction import Extraction
from ember.entity import Entity
from ember.memory_lifecycle import Lifecycle
from ember.source_events import events_for_sources


class ExtractionPageTests(unittest.TestCase):
    setUp = fixtures.ExtractionTests.setUp
    source = fixtures.ExtractionTests.source
    discover = fixtures.ExtractionTests.discover

    def begin(self, source, identity='extraction-page-test'):
        body = {'maintenance_id': identity, 'session_id': 'old',
                'assistant_message_ids': [int(source['assistant_message_id'])]}
        with patch('ember.source_sync.sync_increment', return_value={'status': 'ok'}):
            result = pages.start(self.service, body)
        self.assertEqual(result['status'], 'ok', result)
        return body

    def call(self, body, name, args, call_id):
        return self.service.sidecar_call_tool(name, args,
            recall_id=body['maintenance_id'], tool_call_id=call_id)

    def test_missing_sources_never_report_complete(self):
        result = pages.status(self.service, {'session_id': 'old', 'assistant_message_ids': [99]})
        self.assertFalse(result['complete'])
        self.assertEqual(result['unknown_message_ids'], [99])

    def test_pending_status_tracks_durable_source_results_and_restart(self):
        first = self.source(1)
        second = self.source(2)
        self.discover()
        scope = {'session_id': 'old', 'assistant_message_ids': [4, 6]}
        self.assertEqual(pages.status(self.service, scope)['pending_count'], 2)
        self.queue.record_sources([first], 'event-1', 'written')
        self.assertEqual(pages.status(self.service, scope)['pending_count'], 1)
        Extraction(self.service).record_sources([second], '', 'skipped')
        self.assertTrue(pages.status(self.service, scope)['complete'])

    def test_scoped_batch_and_skip_cannot_consume_another_page(self):
        first = self.source(1)
        second = self.source(2)
        self.discover()
        body = self.begin(first)
        batch = self.call(body, 'get_memory_extraction_batch', {}, 'batch')
        self.assertEqual({source['slice_id'] for group in batch['groups'] for source in group['slices']}, {first['slice_id']})
        rejected = self.call(body, 'finish_memory_extraction_group',
            {'group_id': batch['groups'][0]['group_id'], 'source_slice_ids': [second['slice_id']]}, 'outside')
        self.assertEqual(rejected['code'], 'extraction_scope_violation')
        skipped = self.call(body, 'finish_memory_extraction_group',
            {'group_id': batch['groups'][0]['group_id']}, 'skip')
        self.assertEqual(skipped['status'], 'ok')
        self.assertTrue(pages.finish(self.service, body)['complete'])
        self.assertEqual(pages.status(self.service, {'session_id': 'old', 'assistant_message_ids': [6]})['pending_count'], 1)

    def test_scoped_generation_writes_event_from_originals_without_fake_messages(self):
        first = self.source(1)
        self.discover()
        body = self.begin(first)
        session = self.service.get_or_create_session('old')
        before = list(session.messages)
        args = {'event': {'summary': 'page decision', 'core': 'a concrete decision',
                'kind': 'daily', 'significance': 8, 'focus': first['messages'][0]['content']},
                'source_slice_ids': [first['slice_id']],
                'recall_routes': [{'cue_type': 'lexical', 'cue': 'decision'}]}
        self.service.entity_store.upsert(Entity('project', 'Deliz'))
        with Lifecycle(self.service.store.root).db() as conn:
            conn.execute("INSERT INTO aliases VALUES ('old-project','project')")
        args['entity'] = {'state': 'linked', 'ids': ['missing']}
        self.assertEqual(self.call(body, 'write_event', args, 'invalid')['status'], 'failed')
        self.assertEqual(self.service.store.all_events(), [])
        args['entity']['ids'] = ['old-project']
        result = self.call(body, 'write_event', args, 'write')
        self.assertEqual(result['status'], 'created', result)
        self.assertEqual(self.call(body, 'write_event', args, 'write'), result)
        self.assertEqual(len(self.service.store.all_events()), 1)
        self.assertEqual(self.service.store.get_event(result['event_id']).entity_refs, ['project'])
        ids = [int(first[key]) for key in ('user_message_id', 'assistant_message_id')]
        self.assertEqual(events_for_sources(self.service, {'session_id': 'old', 'message_ids': ids})['events'],
                         [{'event_id': result['event_id'], 'title': 'page decision'}])
        self.assertEqual(session.messages, before)
        self.assertTrue(pages.finish(self.service, body)['complete'])
        self.assertTrue(pages.finish(self.service, body)['complete'])

    def test_no_scope_escape_or_unbound_writes_after_finish(self):
        first = self.source(1)
        self.discover()
        body = self.begin(first)
        denied = self.call(body, 'write_event', {'event': {}}, 'quote-only')
        self.assertEqual(denied['code'], 'extraction_scope_violation')
        denied = self.call(body, 'set_knowledge_pin', {'memory_id': 'other', 'pinned': True}, 'pin')
        self.assertEqual(denied['code'], 'extraction_scope_violation')
        pages.finish(self.service, body)
        denied = self.call(body, 'get_memory_extraction_batch', {}, 'late')
        self.assertEqual(denied['code'], 'stale_generation')

    def test_active_ordinary_generation_is_not_superseded(self):
        first = self.source(1)
        self.service.generations.reserve('ordinary', 'old', 'hash', 1)
        with patch('ember.source_sync.sync_increment', return_value={'status': 'ok'}):
            result = pages.start(self.service, {'maintenance_id': 'extraction-busy',
                'session_id': 'old', 'assistant_message_ids': [int(first['assistant_message_id'])]})
        self.assertEqual(result['code'], 'generation_busy')
        self.assertEqual(self.service.generations.generation('ordinary')['state'], 'starting')

    def test_missing_live_maintenance_context_requires_fresh_attempt(self):
        first = self.source(1)
        self.discover()
        body = self.begin(first)
        self.service._generation_contexts.clear()
        result = pages.start(self.service, body)
        self.assertEqual(result['code'], 'extraction_context_lost')
        next_body = self.begin(first, 'extraction-after-restart')
        self.assertEqual(self.service.generations.generation(body['maintenance_id'])['state'], 'interrupted')
        pages.finish(self.service, next_body)

    def test_user_side_of_cut_pair_matches_the_same_pending_source(self):
        source = self.source(1)
        result = pages.status(self.service, {'session_id': 'old', 'message_ids': [int(source['user_message_id'])]})
        self.assertEqual(result['pending_count'], 1)
        self.assertEqual(result['unknown_message_ids'], [])
        self.assertEqual(result['items'][0]['assistant_message_id'], int(source['assistant_message_id']))

    def test_lost_start_reply_can_be_replayed_or_safely_superseded(self):
        source = self.source(1)
        self.discover()
        body = self.begin(source)
        replay = pages.start(self.service, body)
        self.assertEqual(replay['pending_count'], 1)
        self.assertFalse(replay['complete'])
        self.begin(source, 'extraction-new-attempt')
        self.assertEqual(self.service.generations.generation(body['maintenance_id'])['state'], 'interrupted')

    def test_unknown_tool_receipt_is_not_blindly_retried(self):
        source = self.source(1)
        self.discover()
        body = self.begin(source)
        self.service.generations.begin_call(body['maintenance_id'], 'unknown', 'request', 'write_event')
        with patch('ember.source_sync.sync_increment', return_value={'status': 'ok'}):
            result = pages.start(self.service, dict(body, maintenance_id='extraction-unsafe-retry'))
        self.assertEqual(result['code'], 'tool_outcome_unknown')
        self.assertIsNone(self.service.generations.generation('extraction-unsafe-retry'))

    def test_shared_group_page_ack_keeps_other_page_material(self):
        first, second = self.source(1), self.source(2)
        with patch('ember.extraction.ask', return_value={'groups': [{'description': 'mixed pages',
                    'slice_ids': [first['slice_id'], second['slice_id']]}]}):
            self.queue.discover(self.config)
        body = self.begin(first)
        batch = self.call(body, 'get_memory_extraction_batch', {}, 'batch')
        self.call(body, 'finish_memory_extraction_group', {'group_id': batch['groups'][0]['group_id']}, 'finish')
        self.assertTrue(pages.finish(self.service, body)['complete'])
        later = self.queue.batch()
        self.assertEqual({row['slice_id'] for group in later['groups'] for row in group['slices']}, {second['slice_id']})

    def test_ordinary_identity_with_extraction_prefix_is_not_a_maintenance_run(self):
        source = self.source(1)
        self.service.generations.reserve('extraction-ordinary', 'old', 'ordinary-hash', 1)
        with patch('ember.source_sync.sync_increment', return_value={'status': 'ok'}):
            result = pages.start(self.service, {'maintenance_id': 'extraction-new', 'session_id': 'old',
                                               'assistant_message_ids': [int(source['assistant_message_id'])]})
        self.assertEqual(result['code'], 'generation_busy')
        self.assertEqual(self.service.generations.generation('extraction-ordinary')['state'], 'starting')
        self.assertEqual(pages.finish(self.service, {'maintenance_id': 'extraction-ordinary'})['code'], 'unknown_maintenance')

    def test_invalid_source_id_is_rejected(self):
        for ids in ([True], [0], [1, 1]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                pages.scope({'session_id': 'old', 'assistant_message_ids': ids})


if __name__ == '__main__':
    unittest.main()
