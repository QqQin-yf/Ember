import os
import tempfile
import unittest
from unittest.mock import patch

from ember.model_gateway import call_model
from ember.privacy import Privacy, PrivateMaterial
from ember.source_client import SourceUnavailable
from ember.turn_slices import TurnSliceStore


class PrivateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        self.policy = Privacy(self.root)
        self.store = TurnSliceStore(self.root)

    def save(self, user, reply, session='main'):
        return self.store.save(session, [
            {'message_id': user, 'role': 'user', 'content': 'Synthetic question'},
            {'message_id': reply, 'role': 'assistant', 'content': 'Synthetic answer'},
        ])

    def test_backdated_private_range_remains_blocked_after_close(self):
        existing = self.save(10, 11)
        self.policy.change('default', 'main', 'open', 12, 10)
        self.policy.change('default', 'main', 'close', 14)
        self.assertIsNone(self.store.get(existing['slice_id']))
        with self.assertRaisesRegex(SourceUnavailable, 'Private source turn'):
            self.save(13, 15)
        self.assertIsNotNone(self.save(16, 17))
        self.assertIsNotNone(self.save(10, 11, 'other'))

    def test_embedding_delay_and_private_range_block_before_transport(self):
        source = self.save(20, 21)
        with patch('ember.model_gateway._call_model', return_value={}) as send:
            with patch.dict(os.environ, {'EMBER_PRIVATE_DELAY_SECONDS': '3600'}):
                with self.assertRaisesRegex(PrivateMaterial, 'private_delay'):
                    call_model('embed', {'input': 'Synthetic'}, source_root=self.root,
                               sources=[source], delayed=True)
            with patch.dict(os.environ, {'EMBER_PRIVATE_DELAY_SECONDS': '0'}):
                call_model('embed', {'input': 'Synthetic'}, source_root=self.root,
                           sources=[source], delayed=True)
                self.policy.change('default', 'main', 'open', 22, 20)
                with self.assertRaisesRegex(PrivateMaterial, 'private_material'):
                    call_model('embed', {'input': 'Synthetic'}, source_root=self.root,
                               sources=[source], delayed=True)
            self.assertEqual(send.call_count, 1)

    def test_private_event_sources_only_cover_selected_turn(self):
        source = self.save(30, 31)
        self.policy.protect_sources([source])
        self.assertIsNone(self.store.get(source['slice_id']))
        self.assertFalse(self.policy.status('default', 'main')['private'])
        self.assertIsNotNone(self.save(32, 33))
    def test_cutover_rejects_unattributed_material_and_grandfathers_old_event(self):
        import json
        from pathlib import Path

        events = Path(self.root) / 'events.jsonl'
        events.write_text(json.dumps({'event_id': 'old', 'ingest_time': '2026-11-13T10:00:00+00:00'}) + '\n'
                          + json.dumps({'event_id': 'new', 'ingest_time': '2026-11-14T10:00:00+00:00'}) + '\n',
                          encoding='utf-8')
        with patch.dict(os.environ, {'EMBER_PRIVATE_CUTOVER': '2026-11-14T00:00:00+00:00'}):
            with patch('ember.model_gateway._call_model', return_value={}) as send:
                for event_id in (None, 'new'):
                    sources = self.policy.event_sources(event_id, 'default') if event_id else []
                    with self.assertRaisesRegex(PrivateMaterial, 'private_source_unverified'):
                        call_model('embed', {'input': 'Synthetic'}, source_root=self.root, sources=sources)
                call_model('embed', {'input': 'Synthetic'}, source_root=self.root,
                           sources=self.policy.event_sources('old', 'default'))
                self.assertEqual(send.call_count, 1)

    def test_unbound_slice_preview_matches_local_keywords_while_private(self):
        from ember.slice_recall import SliceRecall

        with patch.dict(os.environ, {'EMBER_PRIVATE_DELAY_SECONDS': '0'}):
            visible = self.save(10, 11)
            self.save(20, 21)
            self.policy.change('default', 'main', 'open', 20)
            with patch('ember.model_gateway._call_model') as send:
                recall = SliceRecall(self.root)
                result = recall.match('synthetic question')
                self.assertEqual(result['status'], 'local_keyword')
                trace = recall.trace(result['trace_id'])
                self.assertEqual([row['slice_id'] for row in trace['rankings']], [visible['slice_id']])
                self.assertEqual(recall.match('synthetic question', session_id='main', purpose='automatic')
                                 ['candidate_count'], 0)
                send.assert_not_called()

    def test_cutover_survives_environment_loss(self):
        with patch.dict(os.environ, {'EMBER_PRIVATE_CUTOVER': '2026-11-14T00:00:00+00:00'}):
            original = Privacy(self.root).cutover()
        with patch.dict(os.environ, {}, clear=True):
            restored = Privacy(self.root)
            self.assertEqual(restored.configuration, (original, 'cutover_env_missing'))
            with self.assertRaisesRegex(PrivateMaterial, 'private_source_unverified'):
                call_model('embed', {'input': 'Synthetic'}, source_root=self.root)

    def test_model_transport_holds_no_database_write_lock(self):
        def transport(*args, **kwargs):
            Privacy(self.root).change('default', 'main', 'open', 40)
            return {}
        source = self.save(30, 31)
        with patch('ember.model_gateway._call_model', side_effect=transport):
            call_model('embed', {'input': 'Synthetic'}, source_root=self.root, sources=[source])
        self.assertTrue(Privacy(self.root).active('default', 'main'))

    def test_ambiguous_backdate_still_opens_current_turn(self):
        from types import SimpleNamespace
        from ember.privacy import dispatch
        service = SimpleNamespace(store=SimpleNamespace(root=self.root), source_instance='default')
        with patch.dict(os.environ, {'EMBER_PRIVATE_CUTOVER': '2026-11-14T00:00:00+00:00'}):
            with patch('ember.source_client.iter_source_branch', return_value=[[
                {'message_id': '8', 'content': 'Synthetic repeated'},
                {'message_id': '6', 'content': 'Synthetic repeated'},
            ]]):
                result = dispatch(service, {'action': 'open', 'start_quote': 'repeated'}, 'main', 10)
        self.assertTrue(result['private'])
        self.assertEqual(result['start_message_id'], '10')
        self.assertEqual(result['backdate_reason'], 'private_quote_ambiguous')

    def test_exact_local_read_can_open_an_existing_private_slice(self):
        from ember.knowledge import KnowledgeStore
        store = KnowledgeStore(self.root)
        saved = self.save(10, 11)
        sources = store.sources([saved['slice_id']])
        self.policy.change('default', 'main', 'open', 10)
        with self.assertRaisesRegex(ValueError, 'source_slice_changed_or_missing'):
            store.read_sources(sources)
        self.assertEqual(store.read_sources(sources, include_private=True)[0]['messages'], saved['messages'])

    def test_egress_denial_logs_purpose_and_reason_only(self):
        source = self.save(10, 11)
        self.policy.change('default', 'main', 'open', 10)
        with self.assertLogs('ember.models', level='WARNING') as logs:
            with self.assertRaises(PrivateMaterial):
                call_model('embed', {'input': 'Synthetic payload excluded from logs'},
                           source_root=self.root, sources=[source])
        self.assertEqual(logs.output, ['WARNING:ember.models:model egress denied purpose=embed reason=private_material'])
