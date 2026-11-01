from concurrent.futures import ThreadPoolExecutor
import copy
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from ember.health_check import check_memory
from ember.lifecycle_config import lifecycle_config
from ember.llm import MockLLMClient
from ember.models import Event
from ember.service import EmberService
from ember.store import EmberStore
from ember.timeline import Timeline


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=EmberService(EmberStore(self.tmp.name),MockLLMClient())
        self.service._briefing_source_block=lambda e:e.core
        self.timeline=Timeline(self.service)
        self.cfg=copy.deepcopy(lifecycle_config())
        self.cfg['timeline'].update(upper_chars=100,lower_chars=80,compressed_upper_chars=1000,compressed_lower_chars=800,point_batch=2)
        patcher=patch('ember.timeline.lifecycle_config',side_effect=lambda:self.cfg)
        patcher.start();self.addCleanup(patcher.stop)

    def event(self,index,size=50):
        event=Event(str(index),'v','x','x',f'2026-01-{index:02d}T00:00:00Z','2026-01-01',
                    core=str(index)*size,summary='relationship '+str(index),kind='relationship')
        self.service.store.write_event(event)
        return event

    def modes(self):
        with self.timeline.db() as db:
            return {r['event_id']:r['mode'] for r in db.execute('SELECT * FROM timeline')}

    def test_compress_once_select_with_hysteresis_and_render_without_model(self):
        for index in (1,2,3):self.event(index)
        with patch('ember.timeline.prose',return_value='compressed') as model:
            self.timeline.prepare()
            self.assertEqual(model.call_count,3)
            self.assertEqual(self.modes(),{'1':'compressed','2':'compressed','3':'original'})
            result=self.timeline.render()
            self.assertEqual(model.call_count,3)
            self.assertIn('3'*50,result['text'])
            self.assertEqual(self.timeline.mode('3'),'original')
            self.assertEqual(self.timeline.mode('1'),'compressed')
        self.event(4,size=5)
        with patch('ember.timeline.prose',return_value='short'):
            self.timeline.prepare()
        self.assertEqual(self.modes()['3'],'original')

    def test_points_keep_disjoint_intervals_and_append_new_text(self):
        self.cfg['timeline'].update(compressed_upper_chars=15,compressed_lower_chars=12)
        for index in (1,2,3,4):self.event(index)
        self.service.pin_store.add('2')
        def points(purpose,instruction,material,schema):
            ids=[r['event_id'] for r in material['batch']]
            return {'points':[{'text':'new point '+','.join(ids),'event_ids':ids}]}
        with patch('ember.timeline.prose',return_value='compressed'),patch('ember.timeline.ask',side_effect=points):
            result=self.timeline.render()
            with self.timeline.db() as db:
                first=self.timeline.points(db)[0]
            self.assertEqual(first['event_ids'],['1','3'])
            self.assertEqual(len(first['intervals']),2)
            self.assertNotIn('2026-01',result['text'])
            self.assertIn('2'*50,result['text'])
            self.event(5)
            self.timeline.render()
        with self.timeline.db() as db:
            all_points=self.timeline.points(db)
        self.assertEqual(all_points[0]['text'],first['text'])
        read=self.timeline.read_point(first['text'])
        self.assertEqual([e['event_id'] for e in read['events']],['1','3'])
        self.assertEqual(len(all_points),2)

    def test_compression_failure_falls_back_to_accurate_original_and_health_warns(self):
        self.cfg['timeline'].update(upper_chars=40,lower_chars=30)
        self.event(1)
        with patch('ember.timeline.prose',side_effect=OSError('fixture')):
            result=self.timeline.render()
        self.assertEqual(result['text'],'1'*50)
        self.assertIn('relationship_compression_pending',result['health']['codes'])
        self.assertIn('relationship_timeline_over_budget',result['health']['codes'])

    def test_half_compressed_ratio_is_reported_by_read_only_health_check(self):
        self.cfg['timeline'].update(upper_chars=60,lower_chars=40)
        self.event(1);self.event(2)
        with patch('ember.timeline.prose',return_value='compressed'):
            self.timeline.render()
        before=self.timeline.path.read_bytes()
        report=check_memory(self.tmp.name)
        self.assertIn('relationship_timeline_budget_ratio',{r['code'] for r in report['findings']})
        self.assertEqual(self.timeline.path.read_bytes(),before)
        self.assertNotIn('11111',json.dumps(report))

    def test_briefing_waits_for_inflight_compression(self):
        self.event(1)
        started=threading.Event();release=threading.Event()
        def compress(*args):
            started.set();release.wait(3);return 'compressed'
        with patch('ember.timeline.prose',side_effect=compress) as model,ThreadPoolExecutor(2) as pool:
            background=pool.submit(self.timeline.prepare)
            self.assertTrue(started.wait(2))
            briefing=pool.submit(self.timeline.render)
            self.assertFalse(briefing.done())
            release.set();background.result();result=briefing.result()
        self.assertEqual(model.call_count,1)
        self.assertEqual(result['text'],'1'*50)

    def test_pinned_history_returns_to_original_even_after_points(self):
        self.cfg['timeline'].update(upper_chars=60,lower_chars=40,compressed_upper_chars=15,compressed_lower_chars=12)
        self.event(1);self.event(2)
        with patch('ember.timeline.prose',return_value='compressed'), \
             patch('ember.timeline.ask',return_value={'points':[{'text':'point','event_ids':['1','2']}]}):
            self.timeline.render()
            self.assertEqual(self.modes()['1'],'pointed')
            self.service.pin_store.add('1')
            result=self.timeline.render()
        self.assertEqual(self.modes()['1'],'original')
        self.assertIn('1'*50,result['text'])
