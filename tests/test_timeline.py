import copy
import json
import tempfile
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

    def test_uses_summary_lowest_score_first_and_preserves_ten(self):
        events=[self.event(i) for i in (1,2,3)]
        events[0].significance=10
        events[1].significance=8
        events[2].significance=2
        self.cfg['timeline'].update(upper_chars=130,lower_chars=115)
        result=self.timeline.render()
        self.assertEqual(self.modes(),{'1':'original','2':'original','3':'compressed'})
        self.assertIn('1'*50,result['text'])
        self.assertIn('relationship 3',result['text'])

    def test_points_keep_disjoint_intervals_and_append_new_text(self):
        self.cfg['timeline'].update(compressed_upper_chars=15,compressed_lower_chars=12)
        for index in (1,2,3,4):self.event(index)
        self.service.pin_store.add('2')
        def points(purpose,instruction,material,schema):
            ids=[r['event_id'] for r in material['batch']]
            return {'points':[{'text':'new point '+','.join(ids),'event_ids':ids}]}
        with patch('ember.timeline.ask',side_effect=points):
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

    def test_ten_is_never_compacted_even_over_budget(self):
        self.cfg['timeline'].update(upper_chars=40,lower_chars=30)
        self.event(1).significance=10
        result=self.timeline.render()
        self.assertEqual(result['text'],'1'*50)
        self.assertIn('relationship_timeline_over_budget',result['health']['codes'])

    def test_half_compressed_ratio_is_reported_by_read_only_health_check(self):
        self.cfg['timeline'].update(upper_chars=60,lower_chars=40)
        self.event(1);self.event(2)
        self.timeline.render()
        before=self.timeline.path.read_bytes()
        report=check_memory(self.tmp.name)
        self.assertIn('relationship_timeline_budget_ratio',{r['code'] for r in report['findings']})
        self.assertEqual(self.timeline.path.read_bytes(),before)
        self.assertNotIn('11111',json.dumps(report))

    def test_pin_after_points_preserves_points_and_adds_original_at_its_position(self):
        self.cfg['timeline'].update(upper_chars=60,lower_chars=40,compressed_upper_chars=15,compressed_lower_chars=12)
        self.event(1);self.event(2)
        with \
             patch('ember.timeline.ask',return_value={'points':[{'text':'point','event_ids':['1','2']}]}):
            self.timeline.render()
            self.assertEqual(self.modes()['1'],'pointed')
            with self.timeline.db() as db:
                original_points=self.timeline.points(db)
            self.service.pin_store.add('1')
            with patch('ember.timeline.ask',side_effect=AssertionError('must not rebuild points')):
                result=self.timeline.render()
        self.assertEqual(self.modes()['1'],'pointed')
        with self.timeline.db() as db:
            self.assertEqual(self.timeline.points(db),original_points)
        self.assertIn('point',result['text'])
        self.assertIn('1'*50,result['text'])

    def test_cold_revision_refreshes_cached_summary(self):
        self.event(1)
        self.timeline.prepare()
        self.service.store.get_event('1').summary='updated'
        self.timeline.prepare()
        with self.timeline.db() as db:
            self.assertEqual(db.execute('SELECT compressed FROM timeline').fetchone()[0],'updated')

    def test_still_visible_sources_cannot_be_abstracted_into_immutable_points(self):
        self.cfg['timeline'].update(upper_chars=60,lower_chars=40,compressed_upper_chars=15,compressed_lower_chars=12)
        self.event(1);self.event(2)
        with \
             patch('ember.event_mutation.visible',return_value=True), patch('ember.timeline.ask') as model:
            self.timeline.prepare()
        model.assert_not_called()
        with self.timeline.db() as db:
            self.assertEqual(self.timeline.points(db),[])
        with patch('ember.event_mutation.visible',return_value=False), \
             patch('ember.timeline.ask',return_value={'points':[{'text':'history','event_ids':['1','2']}]}):
            self.timeline.prepare()
        self.assertEqual(self.modes(),{'1':'pointed','2':'pointed'})
