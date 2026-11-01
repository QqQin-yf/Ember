import tempfile
import unittest
from unittest.mock import patch

from ember.knowledge import KnowledgeStore
from ember.llm import MockLLMClient
from ember.service import EmberService
from ember.source_paths import QuoteSourceError
from ember.store import EmberStore
from ember.turn_slices import TurnSliceStore
from tests.test_sidecar import write_args


class FocusQuoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=EmberService(EmberStore(self.tmp.name),MockLLMClient())
        with patch.object(self.service,'_fetched_corpus',return_value=[]):
            self.service.hook_turn_start('s','今天我们决定把传播模型改成 Route Search。',user_message_id=71,recall_id='r')

    def write(self,args):
        return self.service.sidecar_call_tool('write_event',args,recall_id='r',tool_call_id='t')

    def test_author_uses_only_words_and_internal_id_is_persisted(self):
        args=write_args();args['event']['focus']='传播模型改成 Route Search。'
        result=self.write(args)
        self.assertEqual(result['status'],'created',result)
        self.assertEqual(EmberStore(self.tmp.name).get_event(result['event_id']).focus,'71')
        self.assertEqual(args['event']['focus'],'传播模型改成 Route Search。')

    def test_focus_cannot_borrow_words_outside_resolved_spans(self):
        args=write_args();args['event']['focus']='区间之外的一句话'
        result=self.write(args)
        self.assertEqual((result['code'],result['field']),('quote_not_found','event.focus'))
        self.assertIn('逐字',result['hint'])
        self.assertFalse(self.service.store.all_events())

    def test_slice_focus_uses_same_formatting_match_and_returns_message_id(self):
        source=TurnSliceStore(self.tmp.name).save('old',[{'message_id':9,'role':'user','content':'**这是**重要决定。'},
            {'message_id':10,'role':'assistant','content':'好，就这样。'}])
        args=write_args();args.pop('quote_spans');args['source_slice_ids']=[source['slice_id']]
        args['event']['focus']='这是重要决定。'
        result=self.write(args)
        self.assertEqual(result['status'],'created',result)
        self.assertEqual(self.service.store.get_event(result['event_id']).focus,'9')

    def test_slice_failure_keeps_quote_error_shape_and_writes_nothing(self):
        source=TurnSliceStore(self.tmp.name).save('old',[{'message_id':9,'role':'user','content':'实际原话'},
            {'message_id':10,'role':'assistant','content':'实际回复'}])
        args=write_args();args.pop('quote_spans');args['source_slice_ids']=[source['slice_id']]
        args['event']['focus']='不在原文里的话'
        result=self.write(args)
        self.assertEqual((result['code'],result['field']),('quote_not_found','event.focus'))
        self.assertFalse(self.service.store.all_events())
        self.assertIsNone(KnowledgeStore(self.tmp.name).event_sources('EVT-001'))

    def test_focus_ambiguity_and_missing_provider_identity_are_explicit(self):
        from types import SimpleNamespace
        event=SimpleNamespace(focus='重要决定')
        with self.assertRaises(QuoteSourceError) as caught:
            self.service._validate_focus(event,[{'role':'user','content':'重要**决定'},
                {'role':'assistant','content':'**重要**决定'}])
        self.assertEqual(caught.exception.as_result()['field'],'event.focus')
        event.focus='实际原话'
        with self.assertRaises(QuoteSourceError) as caught:
            self.service._validate_focus(event,[{'role':'user','content':'实际原话'}])
        self.assertEqual(caught.exception.code,'source_identity_missing')
