import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ember.knowledge import KnowledgeStore
from ember.entity import Entity
from ember.lifecycle_config import lifecycle_config
from ember.llm import MockLLMClient
from ember.memory_lifecycle import Lifecycle
from ember.models import Event
from ember.service import EmberService
from ember.store import EmberStore
from ember.timeline import Timeline
from ember.turn_slices import TurnSliceStore


def recent_age_cases():
    return [('fresh',timedelta(hours=1),'all-core'),
            ('at48',timedelta(hours=48),'all-core'),
            ('after48',timedelta(hours=48,seconds=1),'score'),
            ('at96',timedelta(hours=96),'score'),
            ('after96',timedelta(hours=96,seconds=1),'excluded'),
            ('future',-timedelta(seconds=1),'excluded')]


class FiveLayerBriefingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=EmberService(EmberStore(self.tmp.name),MockLLMClient())
        self.service.read_source_event=lambda eid,**kwargs:{'status':'ok','transcript':'RAW-'+eid}
        self.cfg=copy.deepcopy(lifecycle_config());self.cfg['briefing']['total_chars']=50000
        for target in ('ember.briefing.lifecycle_config','ember.timeline.lifecycle_config'):
            patcher=patch(target,side_effect=lambda:self.cfg);patcher.start();self.addCleanup(patcher.stop)

    def event(self,eid,kind='daily',score=8,pin=False,due=None):
        now=datetime.now(timezone.utc).isoformat()
        event=Event(eid,'v','x','x',now,now,summary='SUMMARY-'+eid,core='CORE-'+eid,
                    kind=kind,significance=score,due=due)
        self.service.store.write_event(event)
        if pin:self.service.pin_store.add(eid)
        return event

    def knowledge(self,content):
        source=TurnSliceStore(self.tmp.name).save('old',[{'message_id':1,'role':'user','content':'source'},
            {'message_id':2,'role':'assistant','content':'reply'}])
        return KnowledgeStore(self.tmp.name).create('cognition',content,['cue'],[source['slice_id']])['memory_id']

    def refresh(self,**kwargs):
        return self.service.sidecar_briefing_refresh(dict(session_id='current',**kwargs))

    def test_recent_age_tiers_use_ingest_time_and_keep_old_pins(self):
        now=datetime.now(timezone.utc)
        class Frozen(datetime):
            @classmethod
            def now(cls,tz=None):
                return now
        cases=[]
        for label,age,mode in recent_age_cases():
            for score in (1,7,8,10):
                event=self.event(f'{label}-score-{score}-end',score=score)
                event.ingest_time=(now-age).isoformat()
                event.event_time=(now-timedelta(days=30)).isoformat()
                cases.append((event,mode))
        pinned=self.event('old-pin',score=1,pin=True)
        pinned.ingest_time=(now-timedelta(hours=97)).isoformat()
        with patch('ember.briefing.datetime',Frozen):
            report=self.refresh()
        self.assertEqual(report['status'],'ok')
        for event,mode in cases:
            with self.subTest(event=event.event_id):
                if mode=='excluded':
                    self.assertNotIn(event.core,report['text'])
                    self.assertNotIn(event.summary,report['text'])
                    continue
                core=mode=='all-core' or event.significance>=8
                self.assertIn(event.core if core else event.summary,report['text'])
                self.assertNotIn(event.summary if core else event.core,report['text'])
        self.assertIn('RAW-old-pin',report['text'])

    def test_entity_directory_is_a_separate_footer(self):
        self.service.entity_store.upsert(Entity('project', 'Deliz', summary='聊天前端。详细历史。'))
        event = self.event('current')
        event.entity_refs = ['project']
        self.service.store.write_event(event)
        report = self.refresh()
        self.assertIn('CORE-current', report['text'])
        self.assertTrue(report['text'].endswith(report['entity_directory']))
        self.assertIn('Deliz · 聊天前端 · project', report['entity_directory'])
        self.assertNotIn('详细历史', report['entity_directory'])

    def test_entity_directory_reports_truncation(self):
        self.cfg['briefing']['entity_directory_chars'] = 150
        for i in range(8):
            self.service.entity_store.upsert(Entity(str(i), 'Project '+str(i), summary='Long name '*10))
        report = self.refresh()
        self.assertIn('/8', report['entity_directory'])
        self.assertNotIn('全部', report['entity_directory'])

    def test_five_layers_and_score_promise_exclusions(self):
        self.event('relationship','relationship')
        self.event('pin',pin=True)
        self.event('recent-core',score=9);self.event('recent-summary',score=6)
        self.service.store.get_event('recent-summary').ingest_time=(datetime.now(timezone.utc)-timedelta(hours=60)).isoformat()
        self.event('ten',score=10)
        old=self.event('old-ten',score=10)
        old.ingest_time=old.event_time=(datetime.now(timezone.utc)-timedelta(days=4)).isoformat()
        self.service.store.write_event(old)
        self.event('future','promise',due=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat())
        self.event('no-due','promise')
        self.event('due','promise',due=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
        self.event('pinned-promise','promise',10,pin=True)
        self.knowledge('KNOWLEDGE-CONTENT')
        Timeline(self.service).prepare()
        report=self.refresh()
        text=report['text']
        for earlier,later in [('CORE-relationship','RAW-pin'),('RAW-pin','CORE-recent-core'),
                              ('CORE-recent-core','KNOWLEDGE-CONTENT'),('KNOWLEDGE-CONTENT','CORE-due')]:
            self.assertLess(text.index(earlier),text.index(later))
        self.assertEqual(text.count('CORE-relationship'),1)
        self.assertIn('SUMMARY-recent-summary',text);self.assertNotIn('CORE-recent-summary',text)
        self.assertIn('CORE-ten',text)
        self.assertNotIn('SUMMARY-ten',text)
        for excluded in ('CORE-old-ten','CORE-future','CORE-no-due'):
            self.assertNotIn(excluded,text)
        self.assertTrue(text.endswith('还有 1 条进过上下文的约定没处理'))
        self.assertNotIn('sig10',report['stats']);self.assertNotIn('recalled_top',report['stats'])

    def test_app_identity_profiles_are_pinned_and_hide_retired_cognition(self):
        from ember.knowledge_tools import dispatch
        mid = self.knowledge('PROFILE-COGNITION')
        args = {'subject':'assistant','overview':'APP-OVERVIEW'+'x'*388+'HIDDEN-TAIL','cognition_ids':[mid]}
        self.assertEqual(dispatch(self.service,'set_identity_profile',args)['status'],'failed')
        receipt=dispatch(self.service,'set_identity_profile',args,{})
        self.assertEqual(receipt['status'],'ok')
        self.assertEqual(receipt['chars'],411)
        self.assertEqual(receipt['visible_chars'],400)
        self.assertIsNotNone(datetime.fromisoformat(receipt['updated_at']).tzinfo)
        self.assertIn('/clear',receipt['next_briefing'])
        first = self.refresh()
        self.assertEqual([p['subject'] for p in first['identity_profiles']],['user','assistant'])
        self.assertIn('APP-OVERVIEW',first['text'])
        self.assertNotIn('HIDDEN-TAIL',first['text'])
        self.assertNotIn('HIDDEN-TAIL',first['identity_profiles'][1]['overview'])
        self.assertTrue(KnowledgeStore(self.tmp.name).identity_profiles()[1]['overview'].endswith('HIDDEN-TAIL'))
        self.assertEqual(first['identity_profiles'][1]['cognition_ids'],[mid])
        Lifecycle(self.tmp.name).mark(mid,'no_recall','low_confidence',['review'])
        second = self.refresh()
        self.assertNotIn('PROFILE-COGNITION',second['text'])
        self.assertEqual(second['identity_profiles'][1]['cognition_ids'],[])

    def test_token_budget_lends_unused_space_and_excludes_profiles(self):
        from ember.briefing_budget import tokens
        del self.cfg['briefing']['total_chars']
        budget=self.cfg['briefing']['token_budget']
        budget.update(total=160,relationship=40,recent=20,pins=20,
                      knowledge=20,entities=20,promises=20,reserve=20)
        store=KnowledgeStore(self.tmp.name)
        store.set_identity_profile('user','hello '*160,[])
        relationship=self.event('relationship','relationship',score=10)
        relationship.core='relationship '*70
        self.service.store.write_event(relationship)
        report=self.refresh()
        self.assertEqual(report['status'],'ok')
        self.assertGreater(report['relationship_token_budget'],40)
        self.assertIn(relationship.core,report['text'])
        body=report['text'].removeprefix(report['profile_text']+'\n')
        self.assertEqual(report['body_tokens'],tokens(body))
        self.assertLessEqual(tokens(body),160)
        self.assertGreater(tokens(report['text']),tokens(body))

    def test_get_is_frozen_until_next_explicit_refresh(self):
        self.event('pin',pin=True);self.knowledge('OLD-KNOWLEDGE')
        first=self.refresh()
        self.knowledge('NEW-KNOWLEDGE');self.event('new-pin',pin=True)
        self.assertEqual(self.service.sidecar_briefing_get(),first)
        second=self.refresh()
        self.assertIn('NEW-KNOWLEDGE',second['text']);self.assertIn('RAW-new-pin',second['text'])
        self.assertEqual(second['revision'],first['revision']+1)

    def test_forced_layers_survive_budget_and_recent_is_trimmed_low_first(self):
        self.cfg['briefing']['total_chars']=1
        self.event('pin',pin=True);self.event('core',score=9);self.event('summary',score=5)
        self.service.store.get_event('summary').ingest_time=(datetime.now(timezone.utc)-timedelta(hours=60)).isoformat()
        self.event('due','promise',due=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat())
        self.knowledge('KNOWLEDGE')
        report=self.refresh();text=report['text']
        for required in ('RAW-pin','CORE-due','KNOWLEDGE'):
            self.assertIn(required,text)
        self.assertNotIn('CORE-core',text);self.assertNotIn('SUMMARY-summary',text)
        self.assertEqual(report['stats']['dropped'],{'core':1,'summary':1})

    def test_exact_context_ids_exclude_only_fully_present_disjoint_sources(self):
        store=KnowledgeStore(self.tmp.name)
        sources=[]
        for sid,seq in [('current',1),('other',3)]:
            source=TurnSliceStore(self.tmp.name).save(sid,[{'message_id':seq,'role':'user','content':'source'},
                {'message_id':seq+1,'role':'assistant','content':'reply'}])
            sources.append(source['slice_id'])
        self.event('present');self.event('disjoint')
        store.save_event_sources('present',store.sources(sources[:1]))
        store.save_event_sources('disjoint',store.sources(sources))
        text=self.refresh(context_message_ids=[1,2])['text']
        self.assertNotIn('CORE-present',text);self.assertIn('CORE-disjoint',text)

    def test_unreadable_saved_snapshot_returns_an_error(self):
        Path(self.service._briefing_path()).write_text('[]',encoding='utf-8')
        self.assertEqual(self.service.sidecar_briefing_get()['code'],'briefing_snapshot_unreadable')
