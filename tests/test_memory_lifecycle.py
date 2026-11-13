import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ember.memory_lifecycle import Lifecycle, sources_in_context
from ember.memory_maintenance import (MemoryMaintenanceWorker, catalog, validate_result, apply_result,
                                      knowledge_reviews, content_query)
from ember.knowledge import KnowledgeStore
from ember.knowledge_tools import dispatch
from ember.slice_embedding import SliceEmbeddingConfig
from ember.maintenance_client import MaintenanceConfig
from ember.service import EmberService
from ember.store import EmberStore
from ember.llm import MockLLMClient
from ember.entity import Entity
from ember.models import Event
from ember.turn_slices import TurnSliceStore
from tests.legacy_sleep_fixture import SleepKnowledge


class MemoryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.service=EmberService(EmberStore(str(self.root)),MockLLMClient())
        self.life=Lifecycle(self.root)
        self.store=KnowledgeStore(self.root)
        self.slices=TurnSliceStore(str(self.root))
        self.source=self.slices.save('old',[{'message_id':1,'role':'user','content':'I no longer like coffee'},
            {'message_id':2,'role':'assistant','content':'Your preference changed'}])
        self.ids=[self.source['slice_id']]
        self.sources=self.store.sources(self.ids)

    def recalled(self,eid):
        return {'prefix':eid,'presentations':[{'presentation':'event','event_id':eid}],
                'recall_refs':{'events':[{'event_id':eid}]}}

    def test_three_round_presence_replay_new_replacement_and_expiry(self):
        first=self.life.hold('chat',1,self.recalled('A'))
        self.assertEqual(first['presence']['age'],1)
        self.assertEqual(self.life.hold('chat',2,{'prefix':''})['prefix'],'A')
        self.assertEqual(self.life.hold('chat',2,self.recalled('B'))['prefix'],'A')
        self.assertEqual(self.life.hold('chat',3,{'prefix':''})['presence']['age'],3)
        self.assertEqual(self.life.hold('chat',4,{'prefix':''})['prefix'],'')
        self.assertEqual(self.life.hold('chat',5,self.recalled('A'))['prefix'],'A')
        self.assertEqual(self.life.hold('chat',6,self.recalled('B'))['prefix'],'B')
        self.assertEqual(self.life.hold('other',7,{'prefix':''})['prefix'],'')

    def test_no_recall_and_context_invalidate_presence_without_deleting_source(self):
        self.life.hold('chat',1,self.recalled('A'))
        self.life.mark('A','no_recall','outdated',['evidence'])
        self.assertEqual(self.life.hold('chat',2,{'prefix':''})['prefix'],'')
        self.assertIsNotNone(self.slices.get(self.ids[0]))
        self.assertTrue(sources_in_context(self.sources,'old',1,[1,2]))
        self.assertFalse(sources_in_context(self.sources,'old',1,[3,4]))
        self.assertFalse(sources_in_context(self.sources,'different',1,[1,2]))

    def event(self,eid,date,summary):
        event=Event(event_id=eid,version_id='v1',source_ref='test',raw_ref='test',
            event_time=date,ingest_time=date,summary=summary,core=summary)
        self.service.store.write_event(event)
        self.store.save_event_sources(eid,self.sources)
        return event

    def test_recall_hold_preserves_current_slice_trace_and_drops_visible_event(self):
        from ember.models import RecallRoute
        self.event('A','2026-01-01T00:00:00Z','A memorable thing')
        self.service.store.write_route(RecallRoute('route-A','A','lexical','lexical','canary'))
        session = SimpleNamespace(session_id='chat')
        with patch('ember.hybrid_recall.automatic_slice_recall', side_effect=[
                {'slice_recall': {'trace_id': f'trace-{i}'}} for i in (1, 2, 3)]), \
             patch('ember.slice_presentation.append_slice_presentations', side_effect=lambda s,r,q,**kw:r):
            first = self.service.run_recall_pipeline(session, 1, 'canary')
            held = self.service.run_recall_pipeline(session, 2, 'unrelated')
            blocked = self.service.run_recall_pipeline(session, 3, 'canary', visible_event_ids=['A'])
        self.assertEqual(first['presentations'][0]['event_id'], 'A')
        self.assertEqual(held['presentations'][0]['event_id'], 'A')
        self.assertEqual(held['slice_recall']['trace_id'], 'trace-2')
        self.assertEqual(blocked['presentations'], [])
        self.assertEqual(blocked['slice_recall']['trace_id'], 'trace-3')

    def test_temporal_resolution_rejects_reversed_order_and_preserves_original(self):
        self.event('A','2026-01-01T00:00:00Z','Liked coffee')
        self.event('B','2026-02-01T00:00:00Z','No longer likes coffee')
        materials=[r for r in catalog(self.service) if r['kind']=='event']
        payload={'materials':materials,'known_groups':[]}
        relation={'source':'A','target':'B','type':'resolves','reason':'preference changed',
            'evidence':[{'id':'A','quote':'Liked coffee'},{'id':'B','quote':'No longer likes coffee'}]}
        output=validate_result([{'relations':[relation]}],payload)
        apply_result(self.service,payload,output)
        self.assertFalse(self.life.eligible('A'))
        self.assertEqual(self.service.store.get_event('A').summary,'Liked coffee')
        relation.update(source='B',target='A')
        with self.assertRaises(ValueError):
            validate_result([{'relations':[relation]}],payload)

    def test_entity_merge_preserves_aliases_events_and_original_cards(self):
        self.event('A','2026-01-01','Project work')
        for eid,name in [('one','Ember'),('two','Ember system')]:
            self.service.entity_store.upsert(Entity(eid,name,summary='Memory system'))
        self.service.entity_store.save()
        from ember.knowledge_review import patch_members
        patch_members(self.service,None,'one','A')
        materials=[r for r in catalog(self.service) if r['kind']=='entity']
        self.assertEqual(next(r for r in materials if r['id']=='one')['events'][0]['id'],'A')
        payload={'materials':materials,'known_groups':[]}
        merge={'source':'one','target':'two','reason':'same object',
            'evidence':[{'id':'one','quote':'Memory system'},{'id':'two','quote':'Memory system'}]}
        output=validate_result([{'entity_merges':[merge]}],payload)
        apply_result(self.service,payload,output)
        self.assertEqual(self.life.canonical('one'),'two')
        self.assertIn('Ember',self.service.entity_store.get('two').aliases)
        members=next(r for r in catalog(self.service) if r['id']=='two')['events']
        self.assertEqual(members[0]['id'],'A')
        self.assertEqual(self.service.entity_store.get('one').name,'Ember')

    def test_third_party_cannot_score_knowledge_or_forge_evidence(self):
        payload={'materials':[{'id':'K','kind':'knowledge','document':{'content':'coffee'}}],'known_groups':[]}
        label={'id':'K','state':'no_recall','basis':'outdated','reason':'test','evidence':[{'id':'K','quote':'coffee'}]}
        with self.assertRaises(ValueError):
            validate_result([{'labels':[label]}],payload)
        payload['materials'][0]['kind']='slice';label['evidence'][0]['quote']='invented'
        with self.assertRaises(ValueError):
            validate_result([{'labels':[label]}],payload)

    def test_review_threshold_and_unchanged_groups_do_not_reopen(self):
        a=self.store.create('pattern','coffee habit',['coffee'],self.ids)['memory_id']
        b=self.store.create('pattern','beverage habit',['beverage'],self.ids)['memory_id']
        rows=[r for r in catalog(self.service) if r['kind']=='knowledge']
        knowledge_reviews(self.service,rows,{a:[1,0],b:[.64,.7684]})
        self.assertEqual(self.store.groups(),[])
        knowledge_reviews(self.service,rows,{a:[1,0],b:[.8,.6]})
        group=self.store.groups()[0];self.store.finish_group(group['group_id'],'reviewed')
        knowledge_reviews(self.service,rows,{a:[1,0],b:[.8,.6]})
        self.assertEqual(self.store.groups(),[])
        self.assertEqual(len(self.store.groups(False)),1)

    def test_sleep_reviews_require_reading_and_enforce_low_score_evidence(self):
        a=self.store.create('pattern','likes coffee',['coffee'],self.ids)['memory_id']
        b=self.store.create('pattern','coffee preference',['coffee'],self.ids)['memory_id']
        rows=[r for r in catalog(self.service) if r['kind']=='knowledge']
        knowledge_reviews(self.service,rows,{a:[1,0],b:[1,0]})
        stage=SleepKnowledge(self.service);identity={'sleep_id':'review','session_id':'chat'}
        gid=stage.start(identity)['groups'][0]['group_id']
        def call(name,args,cid):
            return stage.call(dict(identity,call_id=cid,name=name,arguments=args))
        args={'group_id':gid,'items':[{'memory_id':a,'action':'keep','confidence':4},
                                    {'memory_id':b,'action':'keep','confidence':8}]}
        self.assertEqual(call('review_memory_knowledge',args,'unread')['status'],'failed')
        self.assertEqual(call('read_memory_slices',{'group_id':gid},'read')['status'],'ok')
        self.assertEqual(call('review_memory_knowledge',args,'no-basis')['status'],'failed')
        args['items'][0].update(confidence_basis='recently_disproved',confidence_evidence='I no longer like coffee')
        result=call('review_memory_knowledge',args,'write')
        self.assertEqual(result['status'],'ok')
        self.assertFalse(self.life.eligible(a))
        self.assertNotIn(a,self.store.briefing()['memory_ids'])
        self.assertEqual(call('review_memory_knowledge',args,'write'),result)

    def test_worker_indexes_raw_slice_without_legacy_model_judgment(self):
        config=SliceEmbeddingConfig('test')
        self.slices.save_embedding(self.source,config.space,[1.,0.])
        reg=SimpleNamespace(names=['default'],service=lambda n:self.service)
        with patch('ember.memory_maintenance.MaintenanceConfig.load',return_value=MaintenanceConfig('https://example.invalid','test','test')), \
             patch('ember.memory_maintenance.SliceEmbeddingConfig.load',return_value=config), \
             patch('ember.memory_maintenance.SliceEmbeddingClient.embed',return_value=[1.,0.]) as embed, \
             patch('ember.memory_maintenance.MaintenanceClient.judge',return_value=[{}]) as judge:
            worker=MemoryMaintenanceWorker(reg)
            self.assertTrue(worker.process_one())
            self.assertFalse(MemoryMaintenanceWorker(reg).process_one())
            judge.assert_not_called()
            self.assertEqual(embed.call_count,0) # reuse the persisted slice vector

    def test_sleep_merges_knowledge_and_old_id_still_drills_to_sources(self):
        a=self.store.create('pattern','coffee choice',['coffee'],self.ids)['memory_id']
        b=self.store.create('pattern','beverage preference',['beverage'],self.ids)['memory_id']
        rows=[r for r in catalog(self.service) if r['kind']=='knowledge']
        knowledge_reviews(self.service,rows,{a:[1,0],b:[1,0]})
        stage=SleepKnowledge(self.service);identity={'sleep_id':'merge','session_id':'chat'}
        gid=stage.start(identity)['groups'][0]['group_id']
        def call(name,args,cid):
            return stage.call(dict(identity,call_id=cid,name=name,arguments=args))
        call('read_memory_slices',{'group_id':gid},'read')
        result=call('review_memory_knowledge',{'group_id':gid,'items':[{'memory_id':a,'action':'merge',
            'merge_ids':[b],'content':'A changing beverage preference','recall_cues':['beverage'],
            'confidence':8}]},'merge')
        self.assertEqual(result['status'],'ok')
        self.assertEqual(self.life.canonical(b),a)
        self.assertFalse(self.life.eligible(b))
        self.assertEqual(dispatch(self.service,'read_memory_slices',{'memory_id':b})['status'],'ok')
        self.assertEqual(next(r for r in self.store.all() if r['memory_id']==b)['content'],'beverage preference')

    def test_entity_extraction_requires_review_material_and_read_sources(self):
        self.event('A','2026-01-01','Working on Ember')
        self.event('B','2026-02-01','Maintaining Ember')
        rows=[r for r in catalog(self.service) if r['kind']=='event']
        gid=self.life.propose('entity_extract',self.sources,'stable project theme',{'items':rows})
        stage=SleepKnowledge(self.service);identity={'sleep_id':'extract','session_id':'chat'}
        stage.start(identity)
        args={'group_id':gid,'name':'Ember','summary':'The memory system project','event_ids':['A','B']}
        body=dict(identity,name='extract_memory_entity',arguments=args)
        self.assertEqual(stage.call(dict(body,call_id='before'))['status'],'failed')
        stage.call(dict(identity,call_id='read',name='read_memory_slices',arguments={'group_id':gid}))
        result=stage.call(dict(body,call_id='extract'))
        self.assertEqual(result['status'],'created')
        ent=next(r for r in catalog(self.service) if r['id']==result['entity_id'])
        self.assertEqual({e['id'] for e in ent['events']},{'A','B'})

    def test_group_extension_does_not_reopen_reviewed_task(self):
        gid=self.life.propose('pattern',self.sources,'existing direction',{})
        self.store.finish_group(gid,'reviewed')
        second=self.slices.save('other',[{'message_id':3,'role':'user','content':'Another coffee experience'},
            {'message_id':4,'role':'assistant','content':'Noted'}])
        ref=self.store.sources([second['slice_id']])
        row={'id':'second','kind':'slice','document':{'messages':[{'content':'Another coffee experience'}]},'sources':ref}
        payload={'materials':[row],'known_groups':self.store.groups(False)}
        result=validate_result([{'extensions':[{'group_id':gid,'ids':['second'],'reason':'same direction',
            'evidence':[{'id':'second','quote':'Another coffee experience'}]}]}],payload)
        apply_result(self.service,payload,result)
        self.assertEqual(self.store.groups(),[])
        self.assertEqual(len(self.store.groups(False)[0]['sources']),2)

    def test_content_vector_spaces_are_not_interchangeable(self):
        config=SliceEmbeddingConfig('test')
        with patch('ember.memory_maintenance.SliceEmbeddingConfig.load',return_value=config), \
             patch('ember.memory_maintenance.SliceEmbeddingClient.embed',return_value=[1,0]) as embed:
            self.assertIsNone(content_query({'model':'vl:wrong-space'},'q',[0,1],{}))
            cache={}
            self.assertEqual(content_query({'model':'vl:'+config.space},'q',[0,1],cache),[1,0])
            self.assertEqual(content_query({'model':'vl:'+config.space},'q',[0,1],cache),[1,0])
            self.assertEqual(embed.call_count,1)

    def test_exact_context_ids_preserve_other_branch_and_filter_present_sources(self):
        from ember.slice_recall import SliceRecall
        from ember.memory_lifecycle import filter_recall
        config=SliceEmbeddingConfig('test');self.slices.save_embedding(self.source,config.space,[1.,0.])
        with patch('ember.slice_recall.SliceEmbeddingConfig.load',return_value=config), \
             patch('ember.slice_recall.SliceEmbeddingClient.embed',return_value=[1.,0.]):
            recall=SliceRecall(str(self.root))
            other=recall.match('query',session_id='old',anchor_message_id=9,context_start_id=1,context_message_ids=[3,4])
            present=recall.match('query',session_id='old',anchor_message_id=9,context_start_id=1,context_message_ids=[1,2])
        self.assertEqual(other['candidate_count'],1)
        self.assertEqual(present['candidate_count'],0)
        self.event('A','2026-01-01','Past experience')
        session=self.service.get_or_create_session('old')
        self.assertNotIn('A',filter_recall(self.service,{},session,1,[3,4]))
        self.assertIn('A',filter_recall(self.service,{},session,1,[1,2]))

    def test_metadata_is_not_valid_relation_evidence(self):
        self.event('A','2026-01-01','Actual experience')
        row=next(r for r in catalog(self.service) if r['id']=='A')
        with self.assertRaises(ValueError):
            validate_result([{'labels':[{'id':'A','state':'no_recall','basis':'meaningless','reason':'test',
                'evidence':[{'id':'A','quote':'2026-01-01'}]}]}],{'materials':[row],'known_groups':[]})


if __name__=='__main__':
    unittest.main()
