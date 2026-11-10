import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ember.slice_reranker import RerankerConfig, SliceReranker, RerankerError
from ember.slice_embedding import SliceEmbeddingConfig
from ember.slice_recall import SliceRecall
from ember.slice_presentation import append_slice_presentations
from ember.event_slices import EventSliceLinks
from ember.turn_slices import TurnSliceStore
from ember.service import EmberService
from ember.store import EmberStore
from ember.llm import MockLLMClient


class SliceRerankerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.config=SliceEmbeddingConfig('synthetic-key')
        self.rerank=RerankerConfig(enabled=True)
        self.store=TurnSliceStore(str(self.root))
        self.sources=[]
        for seq in (1,3,5):
            source=self.store.save('old',[{'message_id':seq,'role':'user','content':'old text '+str(seq)},
                {'message_id':seq+1,'role':'assistant','content':'reply '+str(seq)}])
            self.store.save_embedding(source,self.config.space,[1.,0.])
            self.sources.append(source)
        links=EventSliceLinks(self.root)
        for eid,turn,seq in [('A',1,1),('B',2,3)]:
            links.register({'event_id':eid,'version_id':'v1','source_session_id':'old','source_evidence':'frozen_messages',
                'source_turn_spans':[[turn,turn]],'turn_messages':[{'turn':turn,'user_message_id':str(seq),'assistant_message_id':str(seq+1)}]})
        from ember.models import Event
        from ember.core_semantic import CoreVectorStore
        events=EmberStore(str(self.root))
        for eid in ('A','B'):
            events.write_event(Event(eid,'v1','conversation/session-old','x','2026-01-01','2026-01-01',core=eid+' complete event',kind='daily'))
            CoreVectorStore(str(self.root)).upsert(eid,eid+' complete event',[1.,0.],self.config.space)
        self.recall=SliceRecall(self.root)
        with patch('ember.slice_recall.SliceEmbeddingConfig.load',return_value=self.config), \
                patch('ember.slice_recall.SliceEmbeddingClient.embed',return_value=[1.,0.]):
            self.metadata=self.recall.match('query')
        self.svc=EmberService(EmberStore(str(self.root)),MockLLMClient())

    def ranks(self):
        pool=self.recall.trace(self.metadata['trace_id'])['candidate_pool']
        by_id={s['slice_id']:i for i,s in enumerate(pool)}
        return [{'index':by_id[s['slice_id']],'relevance_score':score} for s,score in zip(self.sources,[.9,.8,.7])]

    def test_vl_reranker_payload_and_untrusted_indices(self):
        response=io.BytesIO(json.dumps({'results':[{'index':0,'relevance_score':.8}]}).encode())
        with patch('ember.model_gateway.urllib.request.urlopen',return_value=response) as http:
            ranks=SliceReranker(self.rerank,self.config).rank('query',[self.sources[0]])
        request=http.call_args.args[0]
        body=json.loads(request.data)
        self.assertEqual(body['model'],'Qwen/Qwen3-VL-Reranker-8B')
        self.assertEqual(request.full_url,'https://api.siliconflow.cn/v1/rerank')
        self.assertEqual(body['documents'],['user:\nold text 1\n\nassistant:\nreply 1'])
        self.assertEqual(ranks[0]['relevance_score'],.8)
        response=io.BytesIO(json.dumps({'results':[{'index':99,'relevance_score':.8}]}).encode())
        with patch('ember.model_gateway.urllib.request.urlopen',return_value=response),self.assertRaises(RerankerError):
            SliceReranker(self.rerank,self.config).rank('query',[self.sources[0]])

    def present(self,refs):
        original={'prefix':'legacy prefix','presentations':[], 'recall_refs':{'events':refs},
                  'recalled_count':3,'slice_recall':self.metadata}
        with patch('ember.slice_presentation.RerankerConfig.load',return_value=self.rerank), \
                patch('ember.slice_presentation.SliceEmbeddingConfig.load',return_value=self.config), \
                patch('ember.slice_presentation.SliceReranker.rank',return_value=self.ranks()) as rerank:
            result=append_slice_presentations(self.svc,original,'query')
        return result,rerank

    def test_three_ownership_cases_leave_legacy_decision_untouched(self):
        result,rerank=self.present([{'event_id':'A'}])
        items=result['presentations']
        self.assertEqual(items[0]['event_id'],'B')
        self.assertEqual(items[1]['presentation'],'source_slice')
        self.assertEqual(items[1]['owner_event_ids'],[])
        self.assertNotIn('old text 1',result['prefix'])
        self.assertNotIn('old text 3',result['prefix'])
        self.assertIn('B complete event',result['prefix'])
        self.assertEqual(len(result['recall_refs']['events']),2)
        self.assertTrue(result['prefix'].startswith('legacy prefix'))
        args=rerank.call_args.args
        self.assertEqual(len(args[1]),3)
        self.assertTrue(all('messages' in s and 'event_id' not in s for s in args[1]))
        trace=self.recall.trace(self.metadata['trace_id'])
        self.assertEqual(len(trace['candidate_pool']),3)
        self.assertEqual(len(trace['rerank']['selected']),2)

    def test_entity_overview_does_not_claim_a_member_event_was_presented(self):
        result,_=self.present([])
        self.assertEqual([p['event_id'] for p in result['presentations'] if p['presentation']=='event_text'],['A','B'])
        self.assertEqual(len(result['recall_refs']['events']),2)

    def test_slice_events_and_attached_entity_text_exclude_raw_slices_and_diagnostics(self):
        from ember.entity import Entity
        from ember.organization_tools import append_entity_cards
        self.svc.entity_store.upsert(Entity('cat', 'Miso', summary='entity summary canary'))
        self.svc.entity_store.save()
        event = self.svc.store.get_event('A')
        event.entity_refs = ['cat']
        self.svc.store.write_event(event)
        result, _ = self.present([])
        result['entity_names'] = []
        result = append_entity_cards(self.svc, result)
        items = result['presentations']
        events = [p for p in items if p['presentation'] == 'event_text']
        self.assertEqual([p['text'] for p in events],
                         ['[A daily] A complete event', '[B daily] B complete event'])
        entity = next(p for p in items if p['presentation'] == 'entity_overview')
        self.assertEqual(entity['entity_id'], 'cat')
        self.assertIn('entity summary canary', entity['text'])
        for item in events + [entity]:
            self.assertIn(item['text'], result['prefix'])
            self.assertNotIn('old text', item['text'])
            self.assertNotIn('reply', item['text'])
        for item in items:
            if item['presentation'] == 'source_slice':
                self.assertNotIn('text', item)
        trace = self.recall.trace(self.metadata['trace_id'])
        for diagnostics in (result['slice_recall'], trace):
            self.assertTrue(all('text' not in p for p in diagnostics['rerank']['selected']))

    def test_reranker_failure_keeps_legacy_prefix_and_candidates(self):
        original={'prefix':'legacy prefix','presentations':[],'recall_refs':{'events':[]},'slice_recall':self.metadata}
        with patch('ember.slice_presentation.RerankerConfig.load',return_value=self.rerank), \
                patch('ember.slice_presentation.SliceEmbeddingConfig.load',return_value=self.config), \
                patch('ember.slice_presentation.SliceReranker.rank',side_effect=RerankerError('reranker_http_429')):
            result=append_slice_presentations(self.svc,original,'query')
        self.assertEqual(result['prefix'],'legacy prefix')
        self.assertEqual(result['slice_recall']['presentation_status'],'unavailable')
        self.assertEqual(len(self.recall.trace(self.metadata['trace_id'])['candidate_pool']),3)

    def test_slice_cannot_reintroduce_visible_parent_event(self):
        original={'prefix':'','presentations':[],'recall_refs':{'events':[]},'slice_recall':self.metadata}
        with patch('ember.slice_presentation.RerankerConfig.load',return_value=self.rerank), \
             patch('ember.slice_presentation.SliceEmbeddingConfig.load',return_value=self.config), \
             patch('ember.slice_presentation.SliceReranker.rank',return_value=[
                 {'index': i, 'relevance_score': 0.95} for i in range(3)]):
            result=append_slice_presentations(self.svc,original,'query',blocked_events={'A'})
        self.assertEqual([p['event_id'] for p in result['presentations'] if p['presentation']=='event_text'],['B'])


if __name__=='__main__':
    unittest.main()
