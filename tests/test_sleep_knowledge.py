import json
import unittest
from unittest.mock import patch

from ember.sleep_knowledge import SleepKnowledge
from ember.knowledge_tools import append_recall, dispatch, tool_specs
import tests.test_pool_knowledge as pool_fixture


class SleepKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=pool_fixture.PoolKnowledgeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.seed_source_review()
        self.service=self.fixture.service
        self.stage=SleepKnowledge(self.service)
        self.identity={'sleep_id':'sleep-1','session_id':'current'}
        self.group=self.fixture.store.groups()[0]

    def call(self,name,args,cid='call-1'):
        return self.stage.call({**self.identity,'call_id':cid,'name':name,'arguments':args})

    def args(self):
        return {'confidence':8,'kind':'pattern','content':'固定午饭选择减少决策负担','recall_cues':['黄焖鸡'],
            'source_slice_ids':[self.fixture.a['slice_id'],self.fixture.b['slice_id']]}

    def read(self):
        result=self.call('read_memory_slices',{'group_id':self.group['group_id']},'read')
        self.assertEqual(result['status'],'ok')
        return result

    def test_normal_turn_does_not_prompt_but_can_author_knowledge(self):
        result=append_recall(self.service,{'prefix':'','presentations':[]},'unrelated')
        self.assertEqual(result['prefix'],'')
        self.assertNotIn('pending_groups',result)
        names={s['function']['name'] for s in tool_specs()}
        self.assertTrue({'read_memory_slices','search_memory_slices','write_memory_knowledge','review_memory_knowledge'}<=names)
        result=dispatch(self.service,'write_memory_knowledge',self.args(),{'session':None,'turn':1})
        self.assertEqual(result['status'],'created')
        self.assertEqual(len(self.fixture.store.all()),1)

    def test_sleep_reads_writes_marks_and_replays_receipt_after_completion(self):
        started=self.stage.start(self.identity)
        self.assertEqual(started,self.stage.start(self.identity))
        self.assertEqual(started['remaining'],1)
        before=self.call('write_memory_knowledge',self.args(),'before-read')
        self.assertEqual(before['code'],'source_not_read_in_sleep')
        self.read()
        created=self.call('write_memory_knowledge',self.args(),'write')
        self.assertEqual(created['status'],'created')
        self.assertEqual(created,self.call('write_memory_knowledge',self.args(),'write'))
        self.assertEqual(len(self.fixture.store.all()),1)
        incomplete=self.stage.finish({**self.identity,'state':'completed'})
        self.assertEqual(incomplete['code'],'sleep_materials_not_finished')
        self.assertEqual(self.call('finish_memory_group',{'group_id':self.group['group_id'],'state':'reviewed'},'ack')['status'],'ok')
        completed=self.stage.finish({**self.identity,'state':'completed'})
        self.assertEqual(completed['state'],'completed')
        self.assertEqual(completed['remaining'],0)
        self.assertEqual(created,self.call('write_memory_knowledge',self.args(),'write'))
        self.assertEqual(self.call('list_memory_groups',{},'new-call')['code'],'sleep_knowledge_not_active')

    def test_context_conflicts_and_other_sources_do_not_write(self):
        self.stage.start(self.identity)
        self.assertEqual(self.stage.start({**self.identity,'session_id':'wrong'})['code'],'sleep_context_conflict')
        result=self.call('write_memory_knowledge',{**self.args(),'source_slice_ids':[self.fixture.current['slice_id']]})
        self.assertEqual(result['code'],'source_outside_sleep_materials')
        result=self.call('finish_memory_group',{'group_id':'foreign','state':'dismissed'},'foreign')
        self.assertEqual(result['code'],'group_outside_sleep_materials')
        self.read()
        created=self.call('write_memory_knowledge',self.args(),'write')
        self.assertEqual(created['status'],'created')
        conflict=self.call('write_memory_knowledge',{**self.args(),'content':'different'},'write')
        self.assertEqual(conflict['code'],'sleep_tool_id_conflict')

    def test_stopped_sleep_keeps_pending_groups_for_next_sleep(self):
        self.stage.start(self.identity)
        self.assertEqual(self.stage.finish({**self.identity,'state':'stopped'})['state'],'stopped')
        next_run=self.stage.start({'sleep_id':'sleep-2','session_id':'current'})
        self.assertEqual(next_run['remaining'],1)

    def test_frozen_source_change_cannot_be_laundered_by_a_slice_id(self):
        self.stage.start(self.identity)
        self.read()
        self.fixture.save('a',1,'modified content')
        result=self.call('write_memory_knowledge',self.args(),'write')
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.fixture.store.all(),[])

    def test_raw_receipt_is_volatile_and_restart_requires_read_again(self):
        self.stage.start(self.identity)
        self.read()
        with self.stage.db() as db:
            row=db.execute('SELECT * FROM sleep_knowledge_calls WHERE call_id=?',('read',)).fetchone()
        self.assertIsNone(row['result_json'])
        self.service._sleep_knowledge_reads={}
        self.service._sleep_knowledge_read_ids={}
        self.assertEqual(self.read_without_assertion()['code'],'result_not_retained')
        self.assertEqual(self.call('write_memory_knowledge',self.args(),'write')['code'],'source_not_read_in_sleep')

    def read_without_assertion(self):
        return self.call('read_memory_slices',{'group_id':self.group['group_id']},'read')

    def test_uncertain_write_never_reexecutes(self):
        self.stage.start(self.identity)
        self.read()
        with patch('ember.sleep_knowledge.dispatch',side_effect=RuntimeError('crash after acceptance')) as execute:
            first=self.call('write_memory_knowledge',self.args(),'write')
            second=self.call('write_memory_knowledge',self.args(),'write')
        self.assertEqual(first['code'],'sleep_tool_outcome_unknown')
        self.assertEqual(second['code'],'sleep_tool_outcome_unknown')
        self.assertEqual(execute.call_count,1)

    def test_saved_output_is_handed_to_next_sleep_after_interruption(self):
        self.stage.start(self.identity)
        self.read()
        created=self.call('write_memory_knowledge',self.args(),'write')
        self.stage.finish({**self.identity,'state':'stopped'})
        next_run=self.stage.start({'sleep_id':'sleep-2','session_id':'current'})
        self.assertEqual(next_run['remaining'],1)
        self.assertEqual(next_run['existing_outputs'][0]['memory_id'],created['memory_id'])

    def test_abandoned_in_progress_call_is_not_forgotten_on_cleanup(self):
        self.stage.start(self.identity)
        with self.stage.db() as db:
            db.execute('INSERT INTO sleep_knowledge_calls VALUES (?,?,?,?,?,?,?)',
                ('sleep-1','interrupted','digest','write_event_from_slices','in_progress',None,0))
        finished=self.stage.finish({**self.identity,'state':'stopped'})
        self.assertEqual(finished['state'],'unknown')
        self.assertEqual(self.stage.start({'sleep_id':'sleep-2','session_id':'current'})['code'],'sleep_knowledge_already_active')


if __name__=='__main__':
    unittest.main()
