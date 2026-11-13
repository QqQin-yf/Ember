import json
import tempfile
import unittest
from unittest.mock import patch

from ember.entity import Entity
from ember.llm import MockLLMClient
from ember.memory_lifecycle import Lifecycle
from ember.models import Event, RecallRoute
from ember.organization import Organization
from ember.organization_tools import dispatch
from ember.service import EmberService
from ember.slice_embedding import SliceEmbeddingConfig
from ember.store import EmberStore


class OrganizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=EmberService(EmberStore(self.tmp.name),MockLLMClient())
        self.org=Organization(self.service);self.cfg=SliceEmbeddingConfig('synthetic')
        for patcher in [patch('ember.organization.SliceEmbeddingConfig.load',return_value=self.cfg),
                        patch('ember.organization.SliceEmbeddingClient.embed',return_value=[1.,0.])]:
            patcher.start();self.addCleanup(patcher.stop)

    def event(self,eid,kind='daily',score=7,core=None):
        ev=Event(eid,'v','x','x','2026-01-01T00:00:00Z','2026-01-01T00:00:00Z',
                 core=core or eid+' concrete experience',summary=eid,kind=kind,significance=score)
        self.service.store.write_event(ev)
        return ev

    def test_daily_pool_is_immutable_and_reaggregation_uses_all_copies(self):
        first=self.event('a');second=self.event('b')
        group={'category':'theme','name':'午饭','event_ids':['a','b']}
        with patch('ember.organization.ask',return_value={'groups':[group]}), \
             patch('ember.organization.prose',return_value='经常一起讨论午饭'):
            self.org.process_one()
        theme=self.org.themes()[0]
        self.assertFalse(Lifecycle(self.tmp.name).eligible('a'))
        self.assertEqual(self.org.replacement('b')['id'],theme['id'])
        first.core='later revision'
        self.org.copy_pool(first)
        third=self.event('c');self.org.copy_pool(third)
        with patch('ember.organization.prose',return_value='新的午饭概括') as model:
            self.org.summarize_theme(dict(group,event_ids=['c'],theme_id=theme['id']),'daily',self.cfg)
        material=model.call_args.args[2]
        self.assertEqual(len(material),3)
        self.assertEqual(next(e['core'] for e in material if e['event_id']=='a'),'a concrete experience')
        self.assertEqual(len(self.org.themes()),1)
        self.assertTrue(self.org.themes()[0]['text'].startswith('来自较早的日常事件的概括'))

    def test_high_score_daily_is_not_replaced(self):
        self.event('a',score=8);self.event('b',score=9)
        group={'category':'theme','name':'主题','event_ids':['a','b']}
        with patch('ember.organization.ask',return_value={'groups':[group]}):
            self.org.process_one()
        self.assertEqual(self.org.themes(),[])
        self.assertTrue(Lifecycle(self.tmp.name).eligible('a'))

    def test_entity_name_only_gives_summary_and_event_hits_deduplicate_card(self):
        self.service.entity_store.upsert(Entity('cat','包子',summary='一只猫'))
        self.service.entity_store.save()
        for eid in ('a','b'):
            event=self.event(eid);event.entity_refs=['cat'];self.service.store.write_event(event)
            self.service.store.write_route(RecallRoute('r'+eid,eid,'lexical','lexical','猫粮'))
        result=self.service.hook_turn_start('s','包子',user_message_id=71,recall_id='one')
        self.assertIn('一只猫',result['injected_text'])
        self.assertNotIn('concrete experience',result['injected_text'])
        self.assertEqual(result['memory']['recalled_count'],0)
        self.service.hook_turn_end('s','收到',assistant_message_id=72,recall_id='one')
        result=self.service.hook_turn_start('s','猫粮',user_message_id=73,recall_id='two')
        self.assertEqual(result['memory']['recalled_count'],2)
        self.assertEqual(result['injected_text'].count('一只猫'),1)
        self.assertEqual(result['memory']['entity_recalled'],1)

    def test_changed_duplicate_merges_into_oldest_and_preserves_old_id(self):
        for eid,created in [('old','2026-01-01'),('new','2026-02-01')]:
            self.service.entity_store.upsert(Entity(eid,eid,summary=eid+' facts',created_at=created))
        self.service.entity_store.save()
        event=self.event('a');event.entity_refs=['new'];self.service.store.write_event(event)
        with self.org.db() as conn:
            conn.execute('INSERT INTO changed_entities VALUES (?)',('new',))
        with patch('ember.organization.ask',return_value={'same_id':'old'}), \
             patch('ember.organization.prose',return_value='合并后的全部事实'):
            self.org.merge_changed(self.cfg)
        self.assertEqual(Lifecycle(self.tmp.name).canonical('new'),'old')
        self.assertEqual(event.entity_refs,['old'])
        self.assertIn('new',self.service.entity_store.get('old').aliases)
        self.assertEqual(self.service.entity_store.get('new').summary,'new facts')

    def test_summary_uses_all_cores_and_originals_and_manual_fix_is_queued(self):
        self.service.entity_store.upsert(Entity('cat','包子',summary='原有事实',entity_type='animal'))
        self.service.entity_store.save();self.event('a')
        earlier = self.event('b'); earlier.entity_refs = ['cat']; self.service.store.write_event(earlier)
        group={'name':'包子','event_ids':['a'],'entity_type':'animal'}
        with patch.object(self.service,'read_source_event',return_value={'transcript':'original'}), \
             patch('ember.organization.ask',return_value={'relations':[]}), \
             patch('ember.organization.prose',return_value='新知道的习惯') as model:
            self.org.attach(group,self.cfg,entity_id='cat')
        self.assertEqual(self.service.entity_store.get('cat').summary,'新知道的习惯')
        self.assertEqual({item['core'] for item in model.call_args.args[2]['events']},
                         {'a concrete experience','b concrete experience'})
        self.assertTrue(all(item['original']=='original' for item in model.call_args.args[2]['events']))
        result=dispatch(self.service,'revise_entity_summary',{'entity_id':'cat','summary':'修正的事实'})
        self.assertEqual(result['status'],'ok')
        read=dispatch(self.service,'read_entity_events',{'entity_id':'cat'})
        self.assertEqual(read['events'][0]['event_id'],'a')
        self.assertNotIn('focus',read['events'][0])

    def test_relationship_never_enters_entity_grouping(self):
        self.event('r',kind='relationship')
        with patch('ember.organization.ask') as model:
            self.assertFalse(self.org.process_one())
        model.assert_not_called()

    def test_linked_backfill_uses_entity_history_without_identity_matching(self):
        self.service.entity_store.upsert(Entity('project', 'Deliz', entity_type='project'))
        later = self.event('later', kind='project')
        later.event_time = '2026-02-01T00:00:00Z'
        later.entity_refs = ['project']
        self.service.store.write_event(later)
        earlier = self.event('earlier', kind='project')
        earlier.entity_refs = ['project']
        earlier.entity_decision = {'state': 'linked', 'ids': ['project']}
        self.service.store.write_event(earlier)
        unrelated = self.event('unrelated', kind='project')
        unrelated.entity_decision = {'state': 'none'}
        self.service.store.write_event(unrelated)

        def ask(purpose, instruction, material, schema):
            if purpose == 'relationship_links':
                self.assertEqual([row['event_id'] for row in material['events']], ['earlier', 'later'])
                return {'relations': [{'source': 'earlier', 'target': 'later', 'type': 'continues'}]}
            self.assertIn('superseded', schema['properties'])
            return {'superseded': []}

        with patch('ember.organization.ask', side_effect=ask), \
             patch('ember.organization.prose', return_value='Deliz current state') as summary:
            self.org.process_one()
            self.org.process_one()
        self.assertEqual(summary.call_count, 1)
        self.assertEqual([(r.src_event, r.dst_event, r.type) for r in self.service.relation_store.all()],
                         [('earlier', 'later', 'continues')])
        self.assertEqual(unrelated.entity_refs, [])

    def test_closed_low_promises_get_separate_objective_theme(self):
        for eid in ('a','b'):
            event=self.event(eid,kind='promise');event.status='done'
            self.service.store.write_event(event);self.org.copy_pool(event)
        with patch('ember.organization.prose',return_value='一起挑过很多次款式'):
            self.org.summarize_theme({'name':'美甲','event_ids':['a','b']},'promise',self.cfg)
        theme=self.org.themes()[0]
        self.assertTrue(theme['text'].startswith('由第三方根据以前的约定客观总结'))
        self.assertIsNone(self.org.replacement('a'))

    def test_project_new_fact_suppresses_old_event_without_deleting_it(self):
        old=self.event('old',kind='project');new=self.event('new',kind='project')
        new.event_time='2026-02-01T00:00:00Z';self.service.store.write_event(new)
        with patch('ember.organization.ask',return_value={'superseded':[{'old':'old','new':'new'}]}):
            self.org.project_supersession([old,new])
        self.assertFalse(Lifecycle(self.tmp.name).eligible('old'))
        self.assertEqual(self.service.store.get_event('old').core,'old concrete experience')
        self.assertTrue(Lifecycle(self.tmp.name).eligible('new'))

    def test_aggregated_daily_slice_still_compares_original_core(self):
        from ember.slice_policy import passing_parents
        first=self.event('a');self.event('b')
        self.org.copy_pool(first);self.org.copy_pool(self.service.store.get_event('b'))
        self.org.vector(first,self.cfg)
        with patch('ember.organization.prose',return_value='主题概括'):
            self.org.summarize_theme({'name':'主题','event_ids':['a','b']},'daily',self.cfg)
        parents=passing_parents(self.tmp.name,{'span_memberships':[{'event_id':'a'}]},[1.,0.],self.cfg,{'a':first})
        self.assertEqual(parents,['a'])
        self.assertIsNotNone(self.org.replacement('a'))
