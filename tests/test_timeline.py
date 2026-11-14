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
        self.cfg['briefing']['recent_hours']=24*365*100
        result=self.timeline.render()
        self.assertEqual(self.modes(),{'1':'original','2':'original','3':'compressed'})
        self.assertIn('1'*50,result['text'])
        self.assertIn('relationship 3',result['text'])

    def test_app_summary_uses_all_sources_and_is_invalidated_on_revision(self):
        from ember.briefing_edits import dispatch
        self.cfg['timeline']['upper_chars']=28
        self.cfg['briefing']['token_budget']['relationship']=5
        for i in range(1,4):
            self.event(i).significance=3
        with patch('ember.lifecycle_config.lifecycle_config', return_value=self.cfg), patch(
            'ember.timeline.ask',side_effect=AssertionError('model called')):
            initial=self.timeline.render(budget=5)
            self.assertTrue(initial['dropped_event_ids'])
            self.assertTrue(initial['summary_task'])
            result=dispatch(self.service,'read_briefing_summary_task',{})
            task=result['task']
            self.assertEqual(len(result['events']),3)
            edit={'revision':0,'operations':[{'action':'group','ids':list(task['sources']),
                  'sources':task['sources'],'task_id':task['task_id'],'text':'history'}]}
            self.assertEqual(dispatch(self.service,'edit_briefing',dict(edit,preview=True))['status'],'preview')
            self.assertEqual(dispatch(self.service,'edit_briefing',edit)['status'],'ok')
            conflict=dispatch(self.service,'edit_briefing',edit)
            self.assertEqual(conflict['code'],'briefing_revision_changed')
            latest=dispatch(self.service,'read_briefing_summary_task',{'event_ids':['1']})
            self.assertEqual(latest['groups'][0]['text'],'history')
            self.assertEqual(self.timeline.render()['text'],'history')
            self.service.store.get_event('2').core='changed'
            revised=self.timeline.render()
            self.assertNotIn('history',revised['text'])
            self.assertEqual(revised['summary_updates'][0]['changed_ids'],['2'])
            self.assertEqual(dispatch(self.service,'edit_briefing',{'revision':1,'undo':True})['status'],'ok')

    def test_window_slides_only_when_over_budget_and_preserves_protected_events(self):
        from ember.memory_lifecycle import Lifecycle
        oldest=self.event(1)
        pinned=self.event(2,size=5)
        next_old=self.event(3)
        remaining=self.event(4)
        full=self.event(5,size=50)
        for event in (oldest,next_old,remaining):
            event.significance=8
            event.summary='summary '+event.event_id*12
        self.service.pin_store.add(pinned.event_id)
        full.significance=10
        self.cfg['timeline']['upper_chars']=150
        self.assertEqual(self.timeline.render()['slid_event_ids'],[])
        self.cfg['timeline']['upper_chars']=80
        report=self.timeline.render()
        self.assertEqual(report['slid_event_ids'],['1','3'])
        self.assertIn(pinned.core,report['text'])
        self.assertIn(full.core,report['text'])
        self.assertIn(remaining.summary,report['text'])
        self.assertLessEqual(report['health']['compressed_and_points_chars']/
                             (report['health']['compressed_and_points_chars']+report['health']['original_chars']),0.3)
        self.assertTrue(Lifecycle(self.tmp.name).eligible(oldest.event_id))
        self.assertEqual(self.timeline.mode(oldest.event_id),'pending')
        self.cfg['timeline']['upper_chars']=150
        self.assertNotIn(oldest.summary,Timeline(self.service).render()['text'])

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

    def test_cold_revision_refreshes_cached_summary(self):
        self.event(1)
        self.timeline.prepare()
        self.service.store.get_event('1').summary='updated'
        self.timeline.prepare()
        with self.timeline.db() as db:
            self.assertEqual(db.execute('SELECT compressed FROM timeline').fetchone()[0],'updated')

    def test_visible_sources_do_not_enter_rendered_summary(self):
        from ember.briefing_edits import dispatch, version
        events=[self.event(i) for i in (1,2)]
        ids=[e.event_id for e in events]
        edit={'revision':0,'operations':[{'action':'group','ids':ids,
               'sources':{e.event_id:version(e) for e in events},'text':'history'}]}
        self.assertEqual(dispatch(self.service,'edit_briefing',edit)['status'],'ok')
        self.assertEqual(self.timeline.render()['text'],'history')
        self.assertNotIn('history',self.timeline.render(lambda e:e.event_id=='1')['text'])
        self.service.pin_store.add('1')
        self.assertNotIn('history',self.timeline.render()['text'])
