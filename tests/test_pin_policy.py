import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from ember.knowledge import KnowledgeStore
from ember.lifecycle_config import lifecycle_config
from ember.llm import MockLLMClient
from ember.memory_lifecycle import Lifecycle
from ember.models import Event
from ember.pin_policy import Pins, centred
from ember.promises import Promises
from ember.service import EmberService
from ember.store import EmberStore
from ember.turn_slices import TurnSliceStore


class PinPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=EmberService(EmberStore(self.tmp.name),MockLLMClient())
        self.pins=Pins(self.service)
        self.cfg=copy.deepcopy(lifecycle_config())
        self.cfg['pin'].update(ordinary_limit=1,relationship_limit=2,knowledge_limit=2,promise_chars=100,event_chars=100,knowledge_chars=80)
        for target in ('ember.pin_policy.lifecycle_config','ember.lifecycle_config.lifecycle_config'):
            patcher=patch(target,side_effect=lambda:self.cfg);patcher.start();self.addCleanup(patcher.stop)

    def event(self,eid,kind='daily',score=8,size=20):
        event=Event(eid,'v','x','x','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',
                    core=eid*size,summary=eid,kind=kind,significance=score,focus='1')
        self.service.store.write_event(event)
        return event

    def test_limits_are_separate_and_full_receipt_includes_that_class(self):
        for eid,kind,score in [('a','daily',8),('b','project',8),('r1','relationship',8),('r2','relationship',8),('r3','relationship',8),('p1','promise',10),('p2','promise',10)]:
            self.event(eid,kind,score)
        self.assertEqual(self.pins.pin('a')['status'],'ok')
        full=self.pins.pin('b')
        self.assertEqual(full['code'],'pin_class_full')
        self.assertEqual([r['event_id'] for r in full['items']],['a'])
        for eid in ('r1','r2','p1','p2'):
            self.assertEqual(self.pins.pin(eid)['status'],'ok')
        self.assertEqual(self.pins.pin('r3')['code'],'pin_class_full')
        listing=self.pins.listing()['classes']
        self.assertEqual(listing['ordinary']['used'],1)
        self.assertEqual(listing['relationship']['used'],2)
        self.assertEqual(len(listing['promise']['items']),2)

    def test_focus_centres_real_source_instead_of_prior_4000_char_prefix(self):
        event=self.event('p','promise',10);event.focus='2'
        corpus=[{'message_id':1,'role':'user','content':'A'*9000,'turn':1,'_source_session_id':'s','_source_message_id':'1'},
                {'message_id':2,'role':'assistant','content':'FOCUS-END','turn':1,'_source_session_id':'s','_source_message_id':'2'}]
        event.source_ref='conversation/session-s';self.service.store.write_event(event)
        self.service.turn_spans.fill('p',[(1,1)],filled_by='test')
        self.service.event_sources.capture('p','v','s',[(1,1)],corpus)
        with patch('ember.service.read_messages',return_value=corpus):
            block=self.pins.event_block(event)
            full=self.service.sidecar_read_memory_source('p')
        self.assertIn('FOCUS-END',block)
        self.assertIn('前面还有',block)
        self.assertNotIn('后面还有',block)
        self.assertGreater(len(full['transcript']),9000)
        self.assertFalse(full['truncated'])
        self.assertNotIn('focus',full)

    def test_focus_window_marks_both_omitted_sides(self):
        messages=[{'message_id':1,'role':'user','content':'A'*200},
                  {'message_id':2,'role':'assistant','content':'CENTRE'},
                  {'message_id':3,'role':'user','content':'B'*200}]
        value=centred(messages,'2',100)
        self.assertIn('CENTRE',value)
        self.assertTrue(value.startswith('前面还有'))
        self.assertTrue(value.endswith('后面还有'))

    def test_hook_reminder_once_until_budget_recovers_then_rearms(self):
        self.event('p','promise',10,size=200);self.pins.pin('p')
        first=self.service.hook_turn_start('s','hello',user_message_id=71,recall_id='one')
        self.assertEqual(len(first['system_reminders']),1)
        self.service.hook_turn_end('s','reply',assistant_message_id=72,recall_id='one')
        second=self.service.hook_turn_start('s','hello',user_message_id=73,recall_id='two')
        self.assertEqual(second['system_reminders'],[])
        self.pins.unpin('p');self.pins.pin('p')
        third=self.service.hook_turn_start('s','hello',user_message_id=75,recall_id='three')
        self.assertEqual(len(third['system_reminders']),1)

    def test_failed_snapshot_does_not_consume_reminder(self):
        self.event('p','promise',10,size=200);self.pins.pin('p')
        with patch.object(self.service.generations,'commit_snapshot',side_effect=OSError('fixture')):
            self.service.hook_turn_start('s','hello',user_message_id=71,recall_id='bad')
        result=self.service.hook_turn_start('s','hello',user_message_id=73,recall_id='good')
        self.assertEqual(len(result['system_reminders']),1)

    def test_merge_keeps_oldest_logs_cross_session_originals_and_pending_exposures(self):
        store=KnowledgeStore(self.tmp.name)
        for eid,sid,seq in [('a','first',1),('b','second',3)]:
            event=self.event(eid,'promise',10)
            if eid=='b':event.ingest_time='2026-02-01T00:00:00Z'
            event.focus=str(seq);event.log=[{'at':eid,'text':'log '+eid,'spans':[]}]
            self.service.store.write_event(event)
            source=TurnSliceStore(self.tmp.name).save(sid,[{'message_id':seq,'role':'user','content':'original '+eid},
                {'message_id':seq+1,'role':'assistant','content':'reply '+eid}])
            store.save_event_sources(eid,store.sources([source['slice_id']]))
            self.pins.pin(eid)
        Promises(self.service).enter('b','cue',{'session_id':'current','message_ids':['71']})
        result=self.pins.merge({'event_ids':['b','a'],'summary':'same promise','core':'merged narrative'})
        self.assertEqual(result['event_id'],'a')
        self.assertEqual(Lifecycle(self.tmp.name).canonical('b'),'a')
        self.assertEqual([r['text'] for r in self.service.store.get_event('a').log],['log a','log b'])
        self.assertEqual(self.service.store.get_event('b').core,'b'*20)
        read=self.service.sidecar_read_memory_source('b')
        self.assertIn('original a',read['transcript']);self.assertIn('original b',read['transcript'])
        self.assertEqual(len(self.pins.events()),1)
        self.assertEqual(Promises(self.service).pending()[0]['event']['event_id'],'a')

    def test_knowledge_count_and_per_item_guard_replace_total_token_limit(self):
        source=TurnSliceStore(self.tmp.name).save('s',[{'message_id':1,'role':'user','content':'source'},
            {'message_id':2,'role':'assistant','content':'reply'}])
        store=KnowledgeStore(self.tmp.name)
        one=store.create('pattern','a'*5000,['a'],[source['slice_id']])
        two=store.create('pattern','b'*5000,['b'],[source['slice_id']])
        three=store.create('pattern','c',['c'],[source['slice_id']])
        self.assertEqual(one['pin']['status'],'ok');self.assertEqual(two['pin']['status'],'ok')
        self.assertEqual(three['pin']['code'],'pin_class_full')
        briefing=store.briefing()
        self.assertNotIn('a'*81,briefing['text'])
        self.assertNotIn('budget_tokens',briefing)
