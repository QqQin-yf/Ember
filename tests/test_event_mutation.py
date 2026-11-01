import tempfile
import unittest
from unittest.mock import patch

from ember.event_mutation import remember_boundary
from ember.llm import MockLLMClient
from ember.memory_lifecycle import Lifecycle
from ember.service import EmberService
from ember.store import EmberStore
from ember.turn_slices import TurnSliceStore
from tests.test_sidecar import write_args

TEXT='今天我们决定把传播模型改成 Route Search。'


class EventMutationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=EmberService(EmberStore(self.tmp.name),MockLLMClient())
        patcher=patch.object(self.service,'_fetched_corpus',return_value=[])
        patcher.start();self.addCleanup(patcher.stop)
        self.service.hook_turn_start('s',TEXT,user_message_id=71,recall_id='r')

    def call(self,name,args,cid='write',rid='r'):
        return self.service.sidecar_call_tool(name,args,recall_id=rid,tool_call_id=cid)

    def create(self,kind='daily',cid='write'):
        args=write_args();args['event']['kind']=kind
        result=self.call('write_promise' if kind=='promise' else 'write_event',args,cid)
        self.assertEqual(result['status'],'created',result)
        return result['event_id']

    def test_same_live_source_can_be_revised_until_cut_and_not_merely_because_recalled(self):
        eid=self.create()
        self.assertEqual(self.call('revise_memory_event',{'event_id':eid,'core':'订正'},'revise')['status'],'revised')
        self.service.hook_turn_end('s','确认',assistant_message_id=75,recall_id='r')
        self.service.hook_turn_start('s','继续',user_message_id=80,recall_id='r2',context_message_ids=[71,75])
        self.assertEqual(self.call('revise_memory_event',{'event_id':eid,'core':'仍在现场'},'before','r2')['status'],'revised')
        report=self.service.sidecar_briefing_refresh({'session_id':'s','keep_from_message_id':80,'context_message_ids':[80]})
        self.assertEqual(report['status'],'ok',report)
        rejected=self.call('revise_memory_event',{'event_id':eid,'core':'不允许'},'after','r2')
        self.assertEqual(rejected['code'],'cooldown_expired')
        self.assertIn('关系事件写一件新的',rejected['hint'])
        self.assertEqual(self.service.store.get_event(eid).core,'仍在现场')

    def test_all_four_kinds_share_source_cooldown(self):
        for index,kind in enumerate(('daily','project','relationship','promise')):
            with self.subTest(kind=kind):
                sid='s'+kind;rid='r'+kind
                self.service.hook_turn_start(sid,TEXT,user_message_id=100+index,recall_id=rid)
                args=write_args();args['event']['kind']=kind
                created=self.call('write_promise' if kind=='promise' else 'write_event',args,'w',rid)
                self.assertEqual(created['status'],'created',created)
                eid=created['event_id']
                self.assertEqual(self.call('revise_memory_event',{'event_id':eid,'core':'允许'},'a',rid)['status'],'revised')
                remember_boundary(self.service,sid,0,[])
                self.assertEqual(self.call('revise_memory_event',{'event_id':eid,'core':'拒绝'},'b',rid)['code'],'cooldown_expired')

    def test_supersedes_keeps_old_content_but_removes_automatic_eligibility(self):
        old=self.create();original=self.service.store.get_event(old).core
        args=write_args();args['supersedes']=[old];args['event']['core']='新的结论'
        result=self.call('write_event',args,'replacement')
        self.assertEqual(result['status'],'created',result)
        prior=EmberStore(self.tmp.name).get_event(old)
        self.assertEqual(prior.core,original)
        self.assertEqual(prior.superseded_by,result['event_id'])
        self.assertFalse(Lifecycle(self.tmp.name).eligible(old))
        self.assertEqual(self.service.sleep_state.status_of(old),'superseded')

    def test_relationship_rejects_supersedes_on_both_sides(self):
        relationship=self.create('relationship')
        for kind,targets in [('relationship',[relationship]),('daily',[relationship])]:
            with self.subTest(kind=kind):
                args=write_args();args['event']['kind']=kind;args['supersedes']=targets
                result=self.call('write_event',args,kind)
                self.assertEqual(result['status'],'failed',result)
                self.assertEqual(len(self.service.store.all_events()),1)

    def test_unknown_target_is_rejected_before_source_capture(self):
        args=write_args();args['supersedes']=['missing']
        with patch.object(self.service.event_sources,'capture') as capture:
            result=self.call('write_event',args)
        self.assertEqual(result['field'],'supersedes[0]')
        capture.assert_not_called();self.assertFalse(self.service.store.all_events())

    def test_slice_writer_also_applies_supersedes(self):
        old=self.create()
        source=TurnSliceStore(self.tmp.name).save('old',[{'message_id':9,'role':'user','content':'新的实际原话'},
            {'message_id':10,'role':'assistant','content':'确认新结论'}])
        args=write_args();args.pop('quote_spans');args['source_slice_ids']=[source['slice_id']]
        args['event']['focus']='新的实际原话';args['supersedes']=[old]
        result=self.call('write_event',args,'slice')
        self.assertEqual(result['status'],'created',result)
        self.assertEqual(self.service.store.get_event(old).superseded_by,result['event_id'])
