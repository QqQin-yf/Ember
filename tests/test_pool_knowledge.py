import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ember.knowledge import KnowledgeStore, knowledge_space, embed_knowledge
from ember.knowledge_tools import dispatch, tool_specs
from ember.pool_maintenance import PoolQueue, PoolMaintenanceWorker
from ember.slice_recall import SliceRecall
from ember.slice_embedding import SliceEmbeddingConfig
from ember.maintenance_client import MaintenanceConfig
from ember.maintenance_materials import prepare
from ember.event_slices import EventSliceLinks
from ember.service import EmberService
from ember.store import EmberStore
from ember.llm import MockLLMClient


class PoolKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.service=EmberService(EmberStore(str(self.root)),MockLLMClient())
        self.store=KnowledgeStore(self.root)
        from ember.turn_slices import TurnSliceStore
        self.slices=TurnSliceStore(str(self.root))
        self.a=self.save('a',1,'午饭经常吃黄焖鸡')
        self.b=self.save('b',3,'黄焖鸡省去选午饭的麻烦')
        self.current=self.save('current',101,'本轮禁止进入维护')
        self.config=SliceEmbeddingConfig('test')

    def save(self,sid,seq,text):
        return self.slices.save(sid,[{'message_id':seq,'role':'user','content':text},
            {'message_id':seq+1,'role':'assistant','content':'reply'}])

    def trace(self,purpose='automatic'):
        return {'trace_id':'trace','instance':'default','session_id':'current','anchor_message_id':'101',
            'purpose':purpose,'candidate_pool':[self.a,self.b,self.current]}

    def judge(self):
        return [{'kind':'pattern','slice_ids':[self.a['slice_id'],self.b['slice_id']],
            'reason':'两段原文体现相近习惯','evidence':[
                {'slice_id':self.a['slice_id'],'quote':'午饭经常吃黄焖鸡'},
                {'slice_id':self.b['slice_id'],'quote':'黄焖鸡省去选午饭的麻烦'}]}]

    def pool_judge(self):
        old=self.judge()[0]
        return [{'proposals':[{'kind':'pattern','direction':'Lunch choice habit','ids':old['slice_ids'],'reason':old['reason'],
            'evidence':[{'id':e['slice_id'],'quote':e['quote']} for e in old['evidence']]}]}]

    def test_pool_is_enqueued_before_any_reply_or_event_and_excludes_current(self):
        queue=PoolQueue(self.root)
        queue.enqueue(self.trace())
        worker=PoolMaintenanceWorker(SimpleNamespace(names=['default'],dir_of=lambda n:str(self.root),service=lambda n:self.service))
        worker.last_discover=float('inf')
        with patch('ember.pool_maintenance.MaintenanceConfig.load',return_value=MaintenanceConfig('https://example.invalid','test','test')), \
                patch('ember.pool_maintenance.MaintenanceClient.judge',return_value=self.pool_judge()) as judge:
            worker.process_one()
        payload=judge.call_args.args[1]
        self.assertEqual(set(payload),{'phase','subject','materials','known_groups'})
        self.assertEqual(len(payload['materials']),2)
        self.assertNotIn('本轮禁止进入维护',json.dumps(payload,ensure_ascii=False))
        self.assertEqual(len(self.store.groups()),1)
        self.assertEqual(self.store.all(),[])
        self.assertEqual(self.service.store.all_events(),[])
        with queue.db() as db:
            self.assertEqual(db.execute('SELECT state FROM pool_jobs').fetchone()[0],'done')

    def test_preview_never_creates_pool_job(self):
        queue=PoolQueue(self.root)
        queue.enqueue(self.trace('preview'))
        self.assertIsNone(queue.claim())

    def test_real_turn_start_enqueues_before_reply_or_memory_write(self):
        for source in (self.a,self.b):
            self.slices.save_embedding(source,self.config.space,[1.,0.])
        from ember.slice_reranker import RerankerConfig
        with patch('ember.slice_recall.SliceEmbeddingConfig.load',return_value=self.config), \
                patch('ember.slice_recall.SliceEmbeddingClient.embed',return_value=[1.,0.]), \
                patch('ember.slice_presentation.RerankerConfig.load',return_value=RerankerConfig()):
            started=self.service.hook_turn_start('current','这轮不参与归类',user_message_id=101,recall_id='pool-born')
        self.assertEqual(started['slice_recall']['candidate_count'],2)
        with PoolQueue(self.root).db() as db:
            job=db.execute('SELECT * FROM pool_jobs').fetchone()
        self.assertEqual(job['state'],'pending')
        self.assertEqual(len(json.loads(job['sources_json'])),2)
        self.assertEqual(self.service.store.all_events(),[])
        self.assertEqual(self.service.generations.generation('pool-born')['state'],'running')

    def test_forged_group_not_saved_and_no_memory_created(self):
        queue=PoolQueue(self.root);queue.enqueue(self.trace())
        worker=PoolMaintenanceWorker(SimpleNamespace(names=['default'],dir_of=lambda n:str(self.root),service=lambda n:self.service))
        worker.last_discover=float('inf')
        decisions=self.pool_judge();decisions[0]['proposals'][0]['ids'][0]='forged'
        with patch('ember.pool_maintenance.MaintenanceConfig.load',return_value=MaintenanceConfig('https://example.invalid','test','test')), \
                patch('ember.pool_maintenance.MaintenanceClient.judge',return_value=decisions):
            worker.process_one()
        self.assertEqual(self.store.groups(),[])
        with queue.db() as db:
            self.assertEqual(db.execute('SELECT state FROM pool_jobs').fetchone()[0],'retry')

    def test_app_authors_knowledge_and_unpin_keeps_cues_and_sources(self):
        self.test_pool_is_enqueued_before_any_reply_or_event_and_excludes_current()
        context={'kind':'sleep','session':self.service.get_or_create_session('current'),'turn':1}
        result=dispatch(self.service,'write_memory_knowledge',{'kind':'pattern','content':'选择固定午饭以减少决策负担',
            'recall_cues':['黄焖鸡'],'source_slice_ids':[self.a['slice_id'],self.b['slice_id']]},context)
        self.assertEqual(result['status'],'created')
        mid=result['memory_id']
        self.assertIn(mid,self.service.sidecar_briefing_get()['knowledge']['memory_ids'])
        self.assertEqual(self.store.match('黄焖鸡'),[])
        dispatch(self.service,'set_knowledge_pin',{'memory_id':mid,'pinned':False},context)
        hits=self.store.match('黄焖鸡')
        self.assertEqual(len(hits),1)
        read=dispatch(self.service,'read_memory_slices',{'memory_id':mid})
        self.assertEqual({r['session_id'] for r in read['slices']},{'a','b'})

    def test_budget_does_not_automatically_unpin_or_delete(self):
        first=self.store.create('cognition','a'*500,['a'],[self.a['slice_id']])
        second=self.store.create('pattern','b'*500,['b'],[self.b['slice_id']])
        self.assertEqual(second['pin']['status'],'budget_review_required')
        self.assertTrue(next(r for r in self.store.all() if r['memory_id']==first['memory_id'])['pinned'])
        self.assertFalse(next(r for r in self.store.all() if r['memory_id']==second['memory_id'])['pinned'])
        self.assertLessEqual(self.store.briefing()['token_upper_bound'],1000)
        self.store.pin(first['memory_id'],False)
        self.assertEqual(self.store.pin(second['memory_id'],True)['status'],'ok')
        self.assertEqual(len(self.store.all()),2)

    def test_vector_threshold_point_five_and_only_highest_extra_hit(self):
        import math
        rows=[]
        for content,vector in [('exact',[.5,math.sqrt(.75)]),('higher',[.8,.6]),('below',[.4999,math.sqrt(1-.4999**2)])]:
            result=self.store.create('pattern',content,['unmatched'],[self.a['slice_id']])
            self.store.pin(result['memory_id'],False)
            row=next(r for r in self.store.all() if r['memory_id']==result['memory_id'])
            with self.store.db() as db:
                db.execute('UPDATE knowledge SET vector_json=?,space=?,vector_hash=? WHERE memory_id=?',
                    (json.dumps(vector),knowledge_space(self.config),row['content_hash'],row['memory_id']))
            rows.append(row)
        hits=self.store.match('query',[1.,0.],knowledge_space(self.config))
        self.assertEqual([r['content'] for r in hits],['higher'])
        with self.store.db() as db:
            db.execute('UPDATE knowledge SET cues_json=? WHERE memory_id=?',(json.dumps(['query']),rows[1]['memory_id']))
        hits=self.store.match('query',[1.,0.],knowledge_space(self.config))
        self.assertEqual([r['content'] for r in hits],['higher'])
        self.store.pin(rows[1]['memory_id'],True)
        hits=self.store.match('query',[1.,0.],knowledge_space(self.config))
        self.assertEqual([r['content'] for r in hits],['exact'])

    def test_background_embedding_then_query_works_without_raw_vectors(self):
        result=self.store.create('cognition','knowledge content',['other'],[self.a['slice_id']])
        self.store.pin(result['memory_id'],False)
        with patch('ember.slice_embedding.SliceEmbeddingClient.embed',return_value=[1.,0.]):
            self.assertTrue(embed_knowledge(self.root,'default',self.config))
        with patch('ember.slice_recall.SliceEmbeddingConfig.load',return_value=self.config), \
                patch('ember.slice_recall.SliceEmbeddingClient.embed',return_value=[1.,0.]) as client:
            result=SliceRecall(self.root).match('query')
        self.assertEqual(client.call_count,1)
        from ember.knowledge_tools import append_recall
        result=append_recall(self.service,{'prefix':'','presentations':[],'slice_recall':result},'query')
        self.assertIn('knowledge content',result['prefix'])

    def test_disjoint_event_write_and_exact_source_read(self):
        from tests.test_sidecar import write_args
        args=write_args()
        args.pop('quote_spans')
        args['source_slice_ids']=[self.a['slice_id'],self.b['slice_id']]
        context={'kind':'sleep','session':self.service.get_or_create_session('current'),'turn':1}
        result=dispatch(self.service,'write_event_from_slices',args,context)
        self.assertEqual(result['status'],'created',result)
        event=self.service.store.get_event(result['event_id'])
        self.assertEqual(self.service.turn_spans.span_of(event.event_id),[])
        source=self.service.read_source_event(event.event_id)
        self.assertEqual({r['session_id'] for r in source['slices']},{'a','b'})
        memberships=EventSliceLinks(self.root).for_slice(self.a['slice_id'])
        self.assertEqual(memberships[0]['membership_kind'],'explicit_source')
        material=prepare(self.root,'default',event.event_id)
        self.assertEqual(material['source_kind'],'slices')

    def test_reviewed_group_does_not_return_on_same_pool_again(self):
        self.test_pool_is_enqueued_before_any_reply_or_event_and_excludes_current()
        group=self.store.groups()[0]
        self.store.finish_group(group['group_id'],'dismissed')
        trace=self.trace();trace['trace_id']='another'
        queue=PoolQueue(self.root);queue.enqueue(trace)
        worker=PoolMaintenanceWorker(SimpleNamespace(names=['default'],dir_of=lambda n:str(self.root),service=lambda n:self.service))
        worker.last_discover=float('inf')
        with patch('ember.pool_maintenance.MaintenanceConfig.load',return_value=MaintenanceConfig('https://example.invalid','test','test')), patch('ember.pool_maintenance.MaintenanceClient.judge',return_value=self.pool_judge()):
            worker.process_one()
        self.assertEqual(self.store.groups(),[])
        self.assertEqual(len(self.store.groups(False)),1)

    def test_source_changes_are_detected_and_instance_cannot_cross(self):
        sources=self.store.sources([self.a['slice_id']])
        self.save('a',1,'changed')
        with self.assertRaises(ValueError):
            self.store.read_sources(sources)
        with self.assertRaises(ValueError):
            KnowledgeStore(self.root,'other').sources([self.b['slice_id']])


if __name__=='__main__':
    unittest.main()
