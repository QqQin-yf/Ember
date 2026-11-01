"""Incremental slicing is independent of App notifications and LLM labels."""
from concurrent.futures import ThreadPoolExecutor
import json
import tempfile
import unittest
from unittest.mock import patch

from ember.core_semantic import CoreVectorStore
from ember.event_slices import EventSliceLinks
from ember.llm import MockLLMClient
from ember.models import Event
from ember.service import EmberService
from ember.store import EmberStore
from ember.slice_embedding import SliceEmbeddingConfig, SliceEmbeddingError
from ember.slice_policy import passing_parents, prepare_slices
from ember.source_sync import db, request_sync, sync_increment
from ember.turn_slices import TurnSliceStore


class IncrementalSliceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=EmberService(EmberStore(self.tmp.name),MockLLMClient())
        self.cfg=SliceEmbeddingConfig('synthetic')
        for patcher in [patch('ember.source_client.configured',return_value=True),
                        patch('ember.source_sync.SliceEmbeddingConfig.load',return_value=self.cfg),
                        patch('ember.slice_policy.SliceEmbeddingClient.embed',side_effect=self.embed)]:
            patcher.start();self.addCleanup(patcher.stop)

    @staticmethod
    def embed(text):
        return [0.,1.] if text in ('好','嗯嗯','晚安','哈哈','好的，知道了','嗯嗯，好呀') else [1.,0.]

    def turn(self,user='晚上吃了麻辣烫',reply='下次还想去',seq=1):
        return {'session_id':'s','messages':[{'role':'user','message_id':seq,'content':user},
                                           {'role':'assistant','message_id':seq+1,'content':reply}]}

    def page(self,cursor,**kwargs):
        return {'cursor':'latest','has_more':False,'turns':[self.turn()] if cursor is None else []}

    def test_two_triggers_share_cursor_under_concurrency(self):
        request_sync(self.service)
        with patch('ember.source_client.fetch_source_increment',side_effect=self.page) as provider:
            with ThreadPoolExecutor(2) as pool:
                results=list(pool.map(lambda _:sync_increment(self.service),range(2)))
        self.assertTrue(all(r['status']=='ok' for r in results),results)
        self.assertEqual([c.args[0] for c in provider.call_args_list],[None,'latest'])
        with TurnSliceStore(self.tmp.name)._db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM turn_slices').fetchone()[0],1)

    def test_failed_embedding_keeps_cursor_for_safe_replay(self):
        request_sync(self.service)
        with patch('ember.source_client.fetch_source_increment',side_effect=self.page), \
             patch('ember.slice_policy.SliceEmbeddingClient.embed',side_effect=SliceEmbeddingError('fixture_failure')):
            self.assertEqual(sync_increment(self.service)['status'],'unavailable')
        with db(self.tmp.name) as conn:
            self.assertIsNone(conn.execute('SELECT cursor FROM sync').fetchone()[0])
        with patch('ember.source_client.fetch_source_increment',side_effect=self.page):
            self.assertEqual(sync_increment(self.service)['status'],'ok')

    def test_empty_orphan_has_no_vector_but_owned_empty_slice_is_kept(self):
        store=TurnSliceStore(self.tmp.name)
        empty=store.save(**self.turn('嗯嗯','好'))
        prepare_slices(self.service,self.cfg)
        self.assertIsNone(store.embedding(empty['slice_id'],self.cfg.space))
        self.event('daily')
        self.service.event_sources.capture('e','v','s',[(1,1)],[
            dict(m,turn=1,_source_session_id='s',_source_message_id=m['message_id']) for m in empty['messages']])
        self.service.turn_spans.fill('e',[(1,1)],filled_by='test')
        prepare_slices(self.service,self.cfg)
        self.assertIsNotNone(store.embedding(empty['slice_id'],self.cfg.space))
        self.assertEqual(EventSliceLinks(self.tmp.name).for_slice(empty['slice_id'])[0]['event_id'],'e')

    def event(self,kind,significance=8,status='open'):
        ev=Event('e','v','conversation/session-s','conversation/session-s/turn-1','2026-01-01','2026-01-01',
                 core='项目代理问题的结论',kind=kind,significance=significance,status=status)
        self.service.store.write_event(ev)
        return ev

    def test_parent_and_slice_compare_separately_and_kind_controls_entry(self):
        candidate={'span_memberships':[{'event_id':'e'}]}
        ev=self.event('daily')
        cores=CoreVectorStore(self.tmp.name)
        cores.upsert('e',ev.core,[0.,1.],self.cfg.space)
        self.assertEqual(passing_parents(self.tmp.name,candidate,[1.,0.],self.cfg,{'e':ev}),[])
        cores.upsert('e',ev.core,[1.,0.],self.cfg.space)
        self.assertEqual(passing_parents(self.tmp.name,candidate,[1.,0.],self.cfg,{'e':ev}),['e'])
        for kind,status,score in [('project','open',8),('relationship','open',8),('promise','done',7)]:
            ev=self.event(kind,score,status)
            self.assertEqual(passing_parents(self.tmp.name,candidate,[1.,0.],self.cfg,{'e':ev}),[])
        ev=self.event('promise',8,'done')
        self.assertEqual(passing_parents(self.tmp.name,candidate,[1.,0.],self.cfg,{'e':ev}),['e'])
