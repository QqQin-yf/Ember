import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ember.event_slices import EventSliceLinks, register_snapshot
from ember.generations import fingerprint
from ember.turn_slices import TurnSliceStore


class EventSliceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TurnSliceStore(self.tmp.name)
        self.links = EventSliceLinks(self.tmp.name)

    def slice(self, user=1, assistant=2, session='s', instance='default', system=False):
        messages = ([{'message_id': user, 'role': 'user', 'content': 'original user'}] if user else [])
        messages.append({'message_id': assistant, 'role': 'assistant', 'content': 'original reply'})
        return self.store.save(session, messages, instance=instance, user_is_system=system)

    def ref(self, eid='E', version='v1', messages=None, spans=None):
        return {'event_id': eid, 'version_id': version, 'source_session_id': 's',
            'source_evidence': 'frozen_messages', 'source_turn_spans': spans or [[1, 1]],
            'turn_messages': messages or [{'turn': 1, 'user_message_id': '1', 'assistant_message_id': '2'}]}

    def test_link_existing_slice_without_reembedding(self):
        source = self.slice()
        self.store.save_embedding(source, 'existing-space', [1., .5])
        with patch('ember.slice_embedding.SliceEmbeddingClient.embed', side_effect=AssertionError('must not embed')):
            self.links.register(self.ref())
        membership = self.links.for_slice(source['slice_id'])[0]
        self.assertEqual((membership['event_id'], membership['turn'], membership['span_start']), ('E', 1, 1))
        self.assertEqual(self.store.embedding(source['slice_id'], 'existing-space')['vector'], [1., .5])

    def test_source_before_slice_automatically_links_when_slice_is_saved(self):
        self.links.register(self.ref())
        source = self.slice()
        self.assertEqual(len(self.links.for_slice(source['slice_id'])), 1)

    def test_one_slice_can_belong_to_multiple_events_and_retry_deduplicates(self):
        source = self.slice()
        for eid in ('A', 'B', 'A'):
            self.links.register(self.ref(eid=eid))
        self.assertEqual({r['event_id'] for r in self.links.for_slice(source['slice_id'])}, {'A', 'B'})

    def test_disconnected_spans_only_use_exact_covered_turns(self):
        sources = [self.slice(1, 2), self.slice(3, 4), self.slice(5, 6)]
        ref = self.ref(spans=[[1, 1], [3, 3]], messages=[
            {'turn': i, 'user_message_id': str(i*2-1), 'assistant_message_id': str(i*2)} for i in (1, 2, 3)])
        self.links.register(ref)
        self.assertEqual([len(self.links.for_slice(s['slice_id'])) for s in sources], [1, 0, 1])

    def test_regenerated_reply_other_session_and_other_instance_do_not_match(self):
        wrong_reply = self.slice(1, 3)
        wrong_session = self.slice(1, 2, session='other')
        wrong_instance = self.slice(1, 2, instance='other')
        self.links.register(self.ref())
        self.assertFalse(self.links.all())
        self.assertFalse(self.links.for_slice(wrong_instance['slice_id'], 'other'))

    def test_pending_assistant_does_not_guess_a_regenerated_reply(self):
        source = self.slice()
        ref = self.ref(messages=[{'turn': 1, 'user_message_id': '1', 'assistant_message_id': None}])
        self.links.register(ref)
        self.assertFalse(self.links.for_slice(source['slice_id']))
        self.links.register(self.ref())
        self.assertEqual(len(self.links.for_slice(source['slice_id'])), 1)

    def test_system_side_omission_and_assistant_only_slice_can_link(self):
        source = self.slice(system=True)
        self.links.register(self.ref())
        self.assertEqual(len(self.links.for_slice(source['slice_id'])), 1)
        only = self.slice(None, 4)
        self.links.register(self.ref(eid='AI', messages=[{'turn': 1, 'user_message_id': None, 'assistant_message_id': '4'}]))
        self.assertEqual(len(self.links.for_slice(only['slice_id'])), 1)

    def test_new_event_version_replaces_old_membership_and_unverified_source_revokes_it(self):
        source = self.slice()
        self.links.register(self.ref())
        self.links.register(self.ref(version='v2'))
        self.assertEqual([r['version_id'] for r in self.links.for_slice(source['slice_id'])], ['v2'])
        bad = self.ref(version='v2');bad['source_evidence'] = 'unverified_corpus'
        result = self.links.register(bad)
        self.assertEqual(result['state'], 'unresolved')
        self.assertFalse(self.links.for_slice(source['slice_id']))

    def snapshot(self):
        source = {'schema_version': 2, 'event_id': 'E', 'version_id': 'v1',
            'source_session_id': 's', 'source_turn_spans': [[1, 1]], 'identity_verified': True,
            'turn_messages': [{'turn': 1, 'user_message_id': '1', 'assistant_message_id': '2'}], 'pending_sources': []}
        return {'events': [{'event_id': 'E', 'version_id': 'v1', 'source_ref': 'conversation/session-s', 'summary': 'summary'}],
                'event_sources': [source], 'turn_spans': [{'event_id': 'E', 'spans': [[1, 1]]}], 'completion_bindings': []}

    def test_frozen_source_version_session_and_span_must_match(self):
        source = self.slice()
        snapshot = self.snapshot()
        self.assertEqual(register_snapshot(self.tmp.name, snapshot)[0]['state'], 'ready')
        self.assertEqual(len(self.links.for_slice(source['slice_id'])), 1)
        for field, value, code in [('version_id', 'v2', 'source_version_mismatch'),
                                    ('source_ref', 'conversation/session-other', 'source_session_mismatch')]:
            bad = copy.deepcopy(snapshot);bad['events'][0][field] = value
            self.assertEqual(register_snapshot(self.tmp.name, bad)[0]['reason'], code)
            self.assertFalse(self.links.for_slice(source['slice_id']))
        bad = copy.deepcopy(snapshot);bad['turn_spans'][0]['spans'] = [[2, 3]]
        self.assertEqual(register_snapshot(self.tmp.name, bad)[0]['reason'], 'span_source_mismatch')

    def test_exact_completed_binding_fills_pending_assistant_without_rewriting_source(self):
        source = self.slice()
        snapshot = self.snapshot();frozen = snapshot['event_sources'][0]
        frozen['turn_messages'][0]['assistant_message_id'] = None
        snapshot['completion_bindings'] = [{'source_hash': fingerprint(frozen), 'turn': 1,
            'binding_json': json.dumps({'turn': 1, 'recall_id': 'r', 'user_message_id': '1',
                'assistant_message_id': '2', 'evidence': 'turn_end', 'status': 'done'})}]
        register_snapshot(self.tmp.name, snapshot)
        self.assertEqual(len(self.links.for_slice(source['slice_id'])), 1)
        self.assertIsNone(frozen['turn_messages'][0]['assistant_message_id'])

    def test_missing_frozen_identity_stays_unresolved(self):
        self.slice()
        snapshot = self.snapshot();snapshot['event_sources'] = []
        result = register_snapshot(self.tmp.name, snapshot)[0]
        self.assertEqual(result['state'], 'unresolved')
        self.assertFalse(self.links.all())

    def test_live_event_files_link_on_slice_save(self):
        snapshot = self.snapshot()
        for key, name in [('events', 'events.jsonl'), ('event_sources', 'event_sources.jsonl'), ('turn_spans', 'turn_spans.jsonl')]:
            Path(self.tmp.name, name).write_text(''.join(json.dumps(r)+'\n' for r in snapshot[key]), encoding='utf-8')
        source = self.slice()
        self.assertEqual(len(self.links.for_slice(source['slice_id'])), 1)

    def test_generation_completion_automatically_adds_pending_membership(self):
        from ember.service import EmberService
        from ember.store import EmberStore
        from ember.llm import MockLLMClient
        from tests.test_generations import TEXT
        from tests.test_sidecar import write_args
        svc = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        source = self.store.save('s', [
            {'message_id': 71, 'role': 'user', 'content': TEXT},
            {'message_id': 75, 'role': 'assistant', 'content': 'confirmed'}])
        svc.hook_turn_start('s', TEXT, user_message_id=71, recall_id='writer')
        result = svc.sidecar_call_tool('write_event', write_args(),
                                      recall_id='writer', tool_call_id='create')
        self.assertEqual(result['status'], 'created')
        self.assertFalse(self.links.for_slice(source['slice_id']))
        svc.hook_turn_end('s', 'confirmed', turn=1, assistant_message_id=75, recall_id='writer')
        self.assertEqual([r['event_id'] for r in self.links.for_slice(source['slice_id'])], [result['event_id']])

    def test_recall_exposes_memberships_without_changing_scores_or_injecting_event(self):
        from ember.slice_embedding import SliceEmbeddingConfig, SliceEmbeddingClient
        from ember.slice_recall import SliceRecall
        config = SliceEmbeddingConfig('synthetic-secret')
        source = self.slice()
        self.store.save_embedding(source, config.space, [1., 0.])
        self.links.register(self.ref())
        with patch.object(SliceEmbeddingConfig, 'load', return_value=config), \
                patch.object(SliceEmbeddingClient, 'embed', return_value=[1., 0.]):
            recall = SliceRecall(self.tmp.name)
            metadata = recall.match('query')
        trace = recall.trace(metadata['trace_id'])
        self.assertEqual(trace['rankings'][0]['score'], 1.)
        self.assertEqual(trace['candidate_pool'][0]['span_memberships'][0]['event_id'], 'E')
        self.assertFalse(trace['injected'])
