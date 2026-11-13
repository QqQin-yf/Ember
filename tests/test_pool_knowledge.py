import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ember.knowledge import KnowledgeStore, knowledge_space, embed_knowledge
from ember.knowledge_tools import dispatch, tool_specs
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

    def seed_source_review(self):
        from ember.memory_lifecycle import Lifecycle
        Lifecycle(self.root).propose('pattern',self.store.sources([self.a['slice_id'],self.b['slice_id']]),
            '两段原文体现相近习惯',{'direction':'Lunch choice habit'})

    def test_proposals_revisit_retired_gemini_sources_but_respect_app_receipts(self):
        from ember.extraction import Extraction
        from ember.memory_lifecycle import Lifecycle
        q = Extraction(self.service)
        life = Lifecycle(self.root)
        for source in (self.a,self.b,self.current):
            self.slices.save_embedding(source,self.config.space,[1.0,0.0])
            q.record_sources([source],'old-gemini','written')
        q.record_sources([self.b],'app-event','written')
        q.record_sources([self.current])
        life.mark('old-gemini','no_recall','retired_legacy_gemini_event',['verified'])
        self.assertEqual(q.candidates(self.config),[])
        self.assertEqual([row['slice_id'] for row in q.candidates(self.config,revisit_legacy=True)],
                         [self.a['slice_id']])

    def test_source_review_fixture_does_not_create_memories(self):
        self.seed_source_review()
        self.assertEqual(len(self.store.groups()),1)
        self.assertEqual(self.store.all(),[])
        self.assertEqual(self.service.store.all_events(),[])

    def test_preview_never_creates_pool_job(self):
        self.slices.save_embedding(self.a,self.config.space,[1.,0.])
        with patch('ember.slice_recall.SliceEmbeddingConfig.load',return_value=self.config), \
             patch('ember.slice_recall.SliceEmbeddingClient.embed',return_value=[1.,0.]), \
             patch('ember.maintenance_client.MaintenanceClient.judge') as judge:
            SliceRecall(self.root).match('preview',purpose='preview')
        judge.assert_not_called()
        with self.store.db() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='pool_jobs'").fetchone())

    def test_real_turn_start_does_not_enqueue_glm_pool_work(self):
        for source in (self.a,self.b):
            self.slices.save_embedding(source,self.config.space,[1.,0.])
        from ember.slice_reranker import RerankerConfig
        with patch('ember.slice_recall.SliceEmbeddingConfig.load',return_value=self.config), \
             patch('ember.slice_recall.SliceEmbeddingClient.embed',return_value=[1.,0.]), \
             patch('ember.slice_presentation.RerankerConfig.load',return_value=RerankerConfig()), \
             patch('ember.maintenance_client.MaintenanceClient.judge') as judge:
            started=self.service.hook_turn_start('current','这轮不参与归类',user_message_id=101,recall_id='pool-born')
        judge.assert_not_called()
        self.assertEqual(started['slice_recall']['candidate_count'],2)
        with self.store.db() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='pool_jobs'").fetchone())
        self.assertEqual(self.service.store.all_events(),[])

    def test_no_pool_classification_tool_can_be_invoked(self):
        with patch('ember.maintenance_client.MaintenanceClient.judge') as judge:
            result=self.service.sidecar_call_tool('classify_recall_pool',{})
        self.assertEqual(result['status'],'failed')
        judge.assert_not_called()

    def test_app_authors_knowledge_and_unpin_keeps_cues_and_sources(self):
        self.seed_source_review()
        context={'kind':'sleep','session':self.service.get_or_create_session('current'),'turn':1}
        result=dispatch(self.service,'write_memory_knowledge',{'kind':'pattern','content':'选择固定午饭以减少决策负担',
            'recall_cues':['黄焖鸡'],'source_slice_ids':[self.a['slice_id'],self.b['slice_id']]},context)
        self.assertEqual(result['status'],'created')
        mid=result['memory_id']
        self.assertIn(mid,self.service.sidecar_briefing_refresh({'session_id':'current'})['knowledge']['memory_ids'])
        self.assertEqual(self.store.match('黄焖鸡'),[])
        dispatch(self.service,'set_knowledge_pin',{'memory_id':mid,'pinned':False},context)
        hits=self.store.match('黄焖鸡')
        self.assertEqual(len(hits),1)
        read=dispatch(self.service,'read_memory_slices',{'memory_id':mid})
        self.assertEqual({r['session_id'] for r in read['slices']},{'a','b'})

    def test_knowledge_pins_use_count_limit_instead_of_old_token_budget(self):
        from ember.lifecycle_config import lifecycle_config
        created=[]
        for index in range(lifecycle_config()['pin']['knowledge_limit']):
            result=self.store.create('pattern',str(index)+'a'*500,['a'],[self.a['slice_id']])
            self.assertEqual(result['pin']['status'],'ok')
            created.append(result['memory_id'])
        overflow=self.store.create('pattern','overflow',['overflow'],[self.a['slice_id']])
        self.assertEqual(overflow['pin']['code'],'pin_class_full')
        self.assertEqual(len(self.store.briefing()['memory_ids']),len(created))
        self.store.pin(created[0],False)
        self.assertEqual(self.store.pin(overflow['memory_id'],True)['status'],'ok')

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
        args['event']['focus']=self.a['messages'][0]['content']
        args['source_slice_ids']=[self.a['slice_id'],self.b['slice_id']]
        context={'kind':'sleep','session':self.service.get_or_create_session('current'),'turn':1}
        result=self.service._dispatch_memory_tool('write_event',args,context)
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
        self.seed_source_review()
        group=self.store.groups()[0]
        self.store.finish_group(group['group_id'],'dismissed')
        self.seed_source_review()
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
