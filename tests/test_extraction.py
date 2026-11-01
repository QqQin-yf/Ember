import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from ember.extraction import Extraction, encoded
from ember.knowledge import KnowledgeStore
from ember.lifecycle_config import lifecycle_config
from ember.llm import MockLLMClient
from ember.memory_lifecycle import Lifecycle
from ember.models import Event
from ember.service import EmberService
from ember.slice_embedding import SliceEmbeddingConfig
from ember.store import EmberStore
from ember.turn_slices import TurnSliceStore


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=EmberService(EmberStore(self.tmp.name),MockLLMClient())
        self.queue=Extraction(self.service);self.slices=TurnSliceStore(self.tmp.name)
        self.config=SliceEmbeddingConfig('fixture-only');self.cfg=copy.deepcopy(lifecycle_config())
        self.cfg['extraction']['batch_chars']=1800
        for target,value in [('ember.extraction.lifecycle_config',lambda:self.cfg),
                             ('ember.extraction.SliceEmbeddingConfig.load',lambda:self.config)]:
            p=patch(target,side_effect=value);p.start();self.addCleanup(p.stop)

    def source(self,n,text=None,vector=True):
        source=self.slices.save('old',[{'message_id':n*2+1,'role':'user','content':text or 'concrete decision '+str(n)},
                                      {'message_id':n*2+2,'role':'assistant','content':'supporting answer'}])
        if vector:self.slices.save_embedding(source,self.config.space,[1.,0.])
        return source

    def discover(self):
        def grouping(purpose,instruction,material,schema):
            self.assertEqual(purpose,'slice_groups')
            return {'groups':[{'description':'same thing','slice_ids':[s['slice_id']]} for s in material['slices']]}
        with patch('ember.extraction.ask',side_effect=grouping) as model:
            self.queue.discover(self.config)
        return model

    def acknowledge(self,part,conclusion='skipped',ids=None):
        return self.queue.finish({'group_id':part['group_id'],'part_id':part['part_id'],
                                  'conclusion':conclusion,'result_ids':ids or []})

    def test_oldest_whole_groups_defer_and_unacknowledged_reappear_after_restart(self):
        first=self.source(1,'first '+('a'*600));self.source(2,'second '+('b'*600))
        self.discover();batch=self.queue.batch()
        self.assertLessEqual(batch['material_chars'],1800)
        self.assertEqual(len(batch['groups']),1)
        self.assertEqual(batch['remaining_groups'],1)
        self.assertEqual(batch['groups'][0]['slices'][0]['slice_id'],first['slice_id'])
        self.assertEqual(Extraction(self.service).batch(),batch)
        self.acknowledge(batch['groups'][0])
        self.assertNotEqual(self.queue.batch()['groups'][0]['group_id'],batch['groups'][0]['group_id'])
        self.assertFalse(self.queue.discover(self.config))

    def test_oversized_single_group_splits_without_text_loss_or_budget_overrun(self):
        self.source(1,'quote " newline\n'+('细节'*2200));self.discover()
        fragments=[];gid=None
        for _ in range(30):
            batch=self.queue.batch()
            if not batch['groups']:break
            self.assertLessEqual(len(encoded(batch['groups'])),1800)
            for part in batch['groups']:
                gid=part['group_id'];fragments.append(part['material_fragment'])
                self.acknowledge(part)
        else:self.fail('split never completed')
        with self.queue.db() as conn:
            payload=conn.execute('SELECT payload FROM groups WHERE id=?',(gid,)).fetchone()[0]
        self.assertEqual(''.join(fragments),payload)
        self.assertEqual(self.queue.batch()['remaining_groups'],0)

    def test_only_informative_ready_orphans_are_sent_and_new_parent_withdraws(self):
        orphan=self.source(1);owned=self.source(2);ignored=self.source(3);self.source(4,vector=False)
        Lifecycle(self.tmp.name).mark(ignored['slice_id'],'no_recall','uninformative_slice',['fixture'])
        ev=Event('parent','v','x','x','2026-01-01','2026-01-01',core='parent',kind='project')
        self.service.store.write_event(ev)
        self.queue.knowledge.save_event_sources('parent',self.queue.knowledge.sources([owned['slice_id']]))
        model=self.discover()
        self.assertEqual([s['slice_id'] for s in model.call_args.args[2]['slices']],[orphan['slice_id']])
        with self.queue.knowledge.db() as conn:
            conn.execute('UPDATE slice_memory_sources SET sources_json=? WHERE memory_id=?',
                         (json.dumps(self.queue.knowledge.sources([owned['slice_id'],orphan['slice_id']])),'parent'))
        self.assertEqual(self.queue.batch()['groups'],[])

    def test_partial_or_duplicate_model_coverage_is_rejected_and_retry_keeps_sources(self):
        source=self.source(1)
        with patch('ember.extraction.ask',return_value={'groups':[]}):
            with self.assertRaises(ValueError):self.queue.discover(self.config)
        self.assertEqual(len(self.queue.candidates(self.config)),1)
        self.discover()
        self.assertEqual(self.queue.batch()['grouping_errors'],0)

    def test_conclusions_require_existing_results_and_preserve_sources(self):
        source=self.source(1);self.discover();part=self.queue.batch()['groups'][0]
        with self.assertRaises(ValueError):self.acknowledge(part,'written')
        created=self.queue.knowledge.create('pattern','concrete decision habit',['decision'],[source['slice_id']])
        result=self.acknowledge(part,'written',[created['memory_id']])
        self.assertEqual(result['state'],'completed')
        self.assertIsNotNone(self.slices.get(source['slice_id']))
        self.assertEqual(self.acknowledge(part,'written',[created['memory_id']]),result)
        with self.assertRaises(ValueError):self.acknowledge(part,'skipped')

    def test_similar_knowledge_is_attached_and_review_works_in_ordinary_generation(self):
        source=self.source(1)
        created=self.queue.knowledge.create('pattern','original decision habit',['decision'],[source['slice_id']])
        self.discover();part=self.queue.batch()['groups'][0]
        self.assertEqual(part['similar_knowledge'][0]['memory_id'],created['memory_id'])
        from ember.knowledge_tools import dispatch
        result=dispatch(self.service,'review_memory_knowledge',{'group_id':part['group_id'],'items':[
            {'memory_id':created['memory_id'],'action':'rewrite','content':'updated decision habit',
             'recall_cues':['decision'],'source_slice_ids':[source['slice_id']],'confidence':8}]},
             {'session':self.service.get_or_create_session('now'),'turn':1})
        self.assertEqual(result['status'],'ok',result)
        self.assertEqual(self.queue.knowledge.all()[0]['content'],'updated decision habit')
        self.acknowledge(part,'merged',[created['memory_id']])

    def test_source_edits_reject_stale_delivery_and_create_new_work(self):
        source=self.source(1);self.discover();part=self.queue.batch()['groups'][0]
        changed=self.source(1,'changed original')
        with self.assertRaises(ValueError):self.acknowledge(part)
        self.assertEqual(self.queue.batch()['groups'],[])
        self.discover();new=self.queue.batch()['groups'][0]
        self.assertNotEqual(new['group_id'],part['group_id'])

    def test_generation_bound_extraction_writes_and_durable_ack_without_raw_receipts(self):
        source=self.source(1);self.discover()
        with patch('ember.slice_recall.SliceEmbeddingConfig.load',return_value=self.config), \
                patch('ember.slice_embedding.SliceEmbeddingClient.embed',return_value=[1.,0.]):
            self.service.hook_turn_start('current','review past decisions',user_message_id=101,recall_id='extract')
        def call(name,args,cid):
            return self.service.sidecar_call_tool(name,args,recall_id='extract',tool_call_id=cid)
        batch=call('get_memory_extraction_batch',{},'read-batch')
        self.assertEqual(batch['status'],'ok',batch)
        part=batch['groups'][0]
        created=call('write_memory_knowledge',{'kind':'pattern','content':'A decision habit',
            'recall_cues':['decisions'],'source_slice_ids':[source['slice_id']],'confidence':8},'write')
        self.assertEqual(created['status'],'created',created)
        args={'group_id':part['group_id'],'part_id':part['part_id'],'conclusion':'written',
              'result_ids':[created['memory_id']]}
        ack=call('finish_memory_extraction_group',args,'ack')
        self.assertEqual(ack['state'],'completed',ack)
        read_receipt=self.service.generations.call('extract','read-batch')
        self.assertNotIn('concrete decision',read_receipt['result_json'] or '')
        restarted=EmberService(EmberStore(self.tmp.name),MockLLMClient())
        self.assertEqual(restarted.sidecar_call_tool('finish_memory_extraction_group',args,
                         recall_id='extract',tool_call_id='ack'),ack)
        self.assertEqual(Extraction(restarted).batch()['groups'],[])
