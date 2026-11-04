"""Actual Event/Entity presentation text is frozen only with the response."""
import json
import tempfile
import unittest
from unittest.mock import patch

from ember.entity import Entity
from ember.generations import SNAPSHOT_TTL_SECONDS
from ember.llm import MockLLMClient
from ember.models import Event, RecallRoute
from ember.service import EmberService
from ember.slice_embedding import SliceEmbeddingConfig
from ember.store import EmberStore


class PresentationTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        for target, value in (
            ('ember.slice_recall.SliceEmbeddingConfig.load', SliceEmbeddingConfig('synthetic')),
            ('ember.slice_recall.SliceEmbeddingClient.embed', [1., 0.]),
        ):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.svc.entity_store.upsert(Entity('cat', 'Miso', summary='entity-original-canary'))
        self.svc.entity_store.save()
        self.svc.store.write_event(Event('A', 'v1', 'x', 'x', '2026-01-01', '2026-01-01',
            core='event-original-canary', entity_refs=['cat'], kind='daily'))
        self.svc.store.write_route(RecallRoute('cue-A', 'A', 'lexical', 'lexical', 'beacon'))

    def start(self, service=None):
        return (service or self.svc).hook_turn_start('s', 'beacon', user_message_id=101, recall_id='r1')

    def test_text_matches_injection_and_legacy_call_remains_compatible(self):
        first = self.start()
        event, entity = first['presentations']
        self.assertEqual(event['event_id'], 'A')
        self.assertEqual(event['version_id'], 'v1')
        self.assertEqual(entity['entity_id'], 'cat')
        self.assertEqual(event['text'], '[A daily 5/10] event-original-canary\n触点：beacon；原文可用 read_memory_source("A") 下钻。')
        self.assertEqual(entity['text'], '关于〈Miso〉 [cat]：entity-original-canary')
        for item in first['presentations']:
            self.assertIn(item['text'], first['injected_text'])
        self.assertNotIn(entity['text'], event['text'])
        self.assertEqual([r['event_id'] for r in first['recall_refs']['events']], ['A'])
        legacy = self.svc.hook_turn_start('legacy', 'beacon')
        for key in ('injected_text', 'memory', 'presentations'):
            self.assertEqual(legacy[key], first[key])

    def test_replay_and_restart_freeze_event_and_entity_after_edits(self):
        first = self.start()
        event = self.svc.store.get_event('A')
        event.core = 'revised event'
        event.version_id = 'v2'
        self.svc.store.write_event(event)
        self.svc.entity_store.get('cat').summary = 'revised entity'
        self.svc.entity_store.save()
        again = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        for service in (self.svc, again):
            with patch.object(service, 'run_recall_pipeline', side_effect=AssertionError('must replay')):
                self.assertEqual(self.start(service), first)
                self.assertEqual(service.sidecar_recall_snapshot('r1', 'response')['response'], first)

    def test_text_does_not_leak_into_refs_or_survive_response_expiry(self):
        with patch('ember.generations.time.time', return_value=1000):
            first = self.start()
            refs = self.svc.sidecar_recall_snapshot('r1')
        self.assertEqual(refs['snapshot']['presentations'], [
            {k: v for k, v in item.items() if k != 'text'} for item in first['presentations']])
        for canary in ('event-original-canary', 'entity-original-canary'):
            self.assertNotIn(canary, json.dumps(refs))
        with patch('ember.generations.time.time', return_value=1000 + SNAPSHOT_TTL_SECONDS):
            self.assertEqual(self.start()['code'], 'response_expired')
            row = self.svc.generations.snapshot('r1')
            self.assertIsNone(row['response_json'])
            self.assertNotIn('original-canary', row['metadata_json'])

    def test_entity_only_and_empty_presentations(self):
        result = self.svc.hook_turn_start('only', 'Miso')
        self.assertEqual(result['presentations'], [{'presentation': 'entity_overview',
            'entity_id': 'cat', 'text': '关于〈Miso〉 [cat]：entity-original-canary'}])
        empty = self.svc.hook_turn_start('empty', 'unrelated')
        self.assertEqual(empty['presentations'], [])
        self.assertIsNone(empty['injected_text'])
