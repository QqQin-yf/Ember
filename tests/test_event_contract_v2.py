"""New Event authoring contract, including rejection before durable effects."""
import copy
import tempfile
import unittest
from unittest.mock import patch

from ember.llm import MockLLMClient
from ember.service import EmberService
from ember.store import EmberStore
from ember.writer import build_tools_spec, validate_writer_arguments
from tests.test_sidecar import write_args


class EventContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.fetch = patch.object(self.service, '_fetched_corpus', return_value=[])
        self.fetch.start()
        self.addCleanup(self.fetch.stop)
        self.service.hook_turn_start('chat', '今天我们决定把传播模型改成 Route Search。',
                                     user_message_id=71, recall_id='r')

    def call(self, name, args):
        return self.service.sidecar_call_tool(name, args, recall_id='r', tool_call_id='t')

    def test_pair_is_adjacent_and_old_names_removed(self):
        names=[s['function']['name'] for s in self.service.sidecar_tool_specs()]
        self.assertEqual(names[:2], ['write_event','write_promise'])
        self.assertFalse({'create_memory_event','write_event_from_slices'} & set(names))
        self.assertTrue(build_tools_spec()[0]['function']['description'].startswith('答应了'))

    def test_promise_initial_state_and_integer_roundtrip(self):
        args=write_args()
        args['event'].update(kind='promise',due='2026-12-01T09:00:00+08:00')
        result=self.call('write_promise',args)
        self.assertEqual(result['status'],'created',result)
        ev=EmberStore(self.tmp.name).get_event(result['event_id'])
        self.assertEqual((ev.kind,ev.status,ev.log,ev.significance),('promise','open',[],8))
        self.assertFalse({'key_quotes','objects','participants','context','state','outcome','affect','details'} & set(ev.to_dict()))

    def test_wrong_tool_cannot_write_a_promise(self):
        args=write_args();args['event']['kind']='promise'
        self.assertEqual(self.call('write_event',args)['status'],'failed')
        self.assertFalse(self.service.store.all_events())

    def test_focus_outside_evidence_rejected_before_capture(self):
        args=write_args();args['event']['focus']='999'
        self.assertEqual(self.call('write_event',args)['status'],'failed')
        self.assertFalse(self.service.store.all_events())
        self.assertFalse(self.service.event_sources.get('EVT-001'))

    def test_sources_are_exclusive_and_removed_fields_rejected(self):
        for mutate in [lambda a:a.update(source_slice_ids=['a']),
                       lambda a:a['event'].update(key_quotes=[]),
                       lambda a:a['event'].update(significance=True),
                       lambda a:a['event'].update(significance='8: reason')]:
            args=copy.deepcopy(write_args());mutate(args)
            self.assertIsNotNone(validate_writer_arguments(args)[1])

