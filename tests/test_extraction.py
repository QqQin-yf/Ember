import copy
import json
import tempfile
import unittest
from unittest.mock import patch

from ember.extraction import Extraction, encoded
from ember.knowledge import KnowledgeStore
from ember.lifecycle_config import lifecycle_config
from ember.llm import MockLLMClient
from ember.memory_lifecycle import Lifecycle
from ember.models import Event
from ember.service import EmberService
from ember.slice_embedding import SliceEmbeddingConfig
from ember.store import EmberStore
from ember.turn_slices import TurnSliceStore


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=EmberService(EmberStore(self.tmp.name),MockLLMClient())
        self.queue=Extraction(self.service);self.slices=TurnSliceStore(self.tmp.name)
        self.config=SliceEmbeddingConfig('fixture-only');self.cfg=copy.deepcopy(lifecycle_config())
        self.cfg['extraction']['batch_chars']=1800
        for target,value in [('ember.extraction.lifecycle_config',lambda:self.cfg),
                             ('ember.extraction.SliceEmbeddingConfig.load',lambda:self.config)]:
            p=patch(target,side_effect=value);p.start();self.addCleanup(p.stop)

    def source(self,n,text=None,vector=True):
        source=self.slices.save('old',[{'message_id':n*2+1,'role':'user','content':text or 'concrete decision '+str(n)},
                                      {'message_id':n*2+2,'role':'assistant','content':'supporting answer'}])
        if vector:self.slices.save_embedding(source,self.config.space,[1.,0.])
        return source

    def discover(self):
        def grouping(purpose,instruction,material,schema):
            self.assertEqual(purpose,'slice_groups')
            return {'groups':[{'description':'same thing','slice_ids':[s['slice_id']]} for s in material['slices']]}
        with patch('ember.extraction.ask',side_effect=grouping) as model:
            self.queue.discover(self.config)
        return model

    def acknowledge(self,part):
        return self.queue.finish({'group_id':part['group_id'],'part_id':part['part_id']})

    def knowledge_write(self,source):
        from ember.knowledge_tools import dispatch
        return dispatch(self.service,'write_memory_knowledge',{'kind':'pattern','content':'A decision habit',
            'recall_cues':['decision'],'source_slice_ids':[source['slice_id']],'confidence':8},
            {'session':self.service.get_or_create_session('current'),'turn':1})

    def grouped_sources(self,*sources):
        with patch('ember.extraction.ask',return_value={'groups':[{'description':'same thing',
                'slice_ids':[s['slice_id'] for s in sources]}]}):
            self.queue.discover(self.config)
        return self.queue.batch()['groups'][0]

    def receipts(self,source):
        with self.queue.db() as conn:
            return [dict(r) for r in conn.execute('SELECT * FROM source_results WHERE slice_id=?',
                                                  (source['slice_id'],))]

    def test_oldest_whole_groups_defer_and_unacknowledged_reappear_after_restart(self):
        first=self.source(1,'first '+('a'*600));self.source(2,'second '+('b'*600))
        self.discover();batch=self.queue.batch()
        self.assertLessEqual(batch['material_chars'],1800)
        self.assertEqual(len(batch['groups']),1)
        self.assertEqual(batch['remaining_groups'],1)
        self.assertEqual(batch['groups'][0]['slices'][0]['slice_id'],first['slice_id'])
        self.assertEqual(Extraction(self.service).batch(),batch)
        self.acknowledge(batch['groups'][0])
        self.assertNotEqual(self.queue.batch()['groups'][0]['group_id'],batch['groups'][0]['group_id'])
        self.assertFalse(self.queue.discover(self.config))

    def test_oversized_single_group_splits_without_text_loss_or_budget_overrun(self):
        self.source(1,'quote " newline\n'+('细节'*2200));self.discover()
        fragments=[];gid=None
        for _ in range(30):
            batch=self.queue.batch()
            if not batch['groups']:break
            self.assertLessEqual(len(encoded(batch['groups'])),1800)
            for part in batch['groups']:
                gid=part['group_id'];fragments.append(part['material_fragment'])
                self.acknowledge(part)
        else:self.fail('split never completed')
        with self.queue.db() as conn:
            payload=conn.execute('SELECT payload FROM groups WHERE id=?',(gid,)).fetchone()[0]
        self.assertEqual(''.join(fragments),payload)
        self.assertEqual(self.queue.batch()['remaining_groups'],0)

    def test_only_informative_ready_orphans_are_sent_and_new_parent_withdraws(self):
        orphan=self.source(1);owned=self.source(2);ignored=self.source(3);self.source(4,vector=False)
        Lifecycle(self.tmp.name).mark(ignored['slice_id'],'no_recall','uninformative_slice',['fixture'])
        ev=Event('parent','v','x','x','2026-01-01','2026-01-01',core='parent',kind='project')
        self.service.store.write_event(ev)
        self.queue.knowledge.save_event_sources('parent',self.queue.knowledge.sources([owned['slice_id']]))
        model=self.discover()
        self.assertEqual([s['slice_id'] for s in model.call_args.args[2]['slices']],[orphan['slice_id']])
        with self.queue.knowledge.db() as conn:
            conn.execute('UPDATE slice_memory_sources SET sources_json=? WHERE memory_id=?',
                         (json.dumps(self.queue.knowledge.sources([owned['slice_id'],orphan['slice_id']])),'parent'))
        self.assertEqual(self.queue.batch()['groups'],[])

    def test_partial_or_duplicate_model_coverage_is_rejected_and_retry_keeps_sources(self):
        source=self.source(1)
        with patch('ember.extraction.time.time',return_value=100),patch('ember.extraction.ask',return_value={'groups':[]}):
            with self.assertRaises(ValueError):self.queue.discover(self.config)
        self.assertEqual(len(self.queue.candidates(self.config)),1)
        with patch('ember.extraction.time.time',return_value=160):
            self.discover()
        self.assertEqual(self.queue.batch()['grouping_errors'],0)

    def test_write_knowledge_automatically_completes_and_keeps_result_and_sources(self):
        source=self.source(1);self.discover();part=self.queue.batch()['groups'][0]
        created=self.knowledge_write(source)
        self.assertEqual(created['status'],'created',created)
        self.assertEqual(self.queue.batch()['groups'],[])
        self.assertEqual(self.receipts(source)[0]['result_id'],created['memory_id'])
        self.assertIsNotNone(self.slices.get(source['slice_id']))
        self.assertEqual(self.queue.finish({'group_id':part['group_id']})['state'],'completed')
        with self.assertRaises(ValueError):
            self.queue.finish({'group_id':part['group_id'],'conclusion':'written'})

    def test_similar_knowledge_is_attached_and_review_works_in_ordinary_generation(self):
        old=self.source(99);source=self.source(1)
        created=self.queue.knowledge.create('pattern','original decision habit',['decision'],[old['slice_id']])
        self.discover();part=self.queue.batch()['groups'][0]
        self.assertEqual(part['similar_knowledge'][0]['memory_id'],created['memory_id'])
        from ember.knowledge_tools import dispatch
        result=dispatch(self.service,'review_memory_knowledge',{'group_id':part['group_id'],'items':[
            {'memory_id':created['memory_id'],'action':'rewrite','content':'updated decision habit',
             'recall_cues':['decision'],'source_slice_ids':[source['slice_id']],'confidence':8}]},
             {'session':self.service.get_or_create_session('now'),'turn':1})
        self.assertEqual(result['status'],'ok',result)
        self.assertEqual(self.queue.knowledge.all()[0]['content'],'updated decision habit')
        self.assertEqual(self.queue.batch()['groups'],[])
        self.assertEqual(self.receipts(source)[0]['result_id'],created['memory_id'])

    def test_source_edits_reject_stale_delivery_and_create_new_work(self):
        source=self.source(1);self.discover();part=self.queue.batch()['groups'][0]
        changed=self.source(1,'changed original')
        with self.assertRaises(ValueError):self.acknowledge(part)
        self.assertEqual(self.queue.batch()['groups'],[])
        self.discover();new=self.queue.batch()['groups'][0]
        self.assertNotEqual(new['group_id'],part['group_id'])

    def test_generation_bound_extraction_writes_and_durable_ack_without_raw_receipts(self):
        source=self.source(1);self.discover()
        with patch('ember.slice_recall.SliceEmbeddingConfig.load',return_value=self.config), \
                patch('ember.slice_embedding.SliceEmbeddingClient.embed',return_value=[1.,0.]):
            self.service.hook_turn_start('current','review past decisions',user_message_id=101,recall_id='extract')
        def call(name,args,cid):
            return self.service.sidecar_call_tool(name,args,recall_id='extract',tool_call_id=cid)
        batch=call('get_memory_extraction_batch',{},'read-batch')
        self.assertEqual(batch['status'],'ok',batch)
        part=batch['groups'][0]
        created=call('write_memory_knowledge',{'kind':'pattern','content':'A decision habit',
            'recall_cues':['decisions'],'source_slice_ids':[source['slice_id']],'confidence':8},'write')
        self.assertEqual(created['status'],'created',created)
        self.assertEqual(self.queue.batch()['groups'],[])
        args={'group_id':part['group_id']}
        ack=call('finish_memory_extraction_group',args,'ack')
        self.assertEqual(ack['state'],'completed',ack)
        read_receipt=self.service.generations.call('extract','read-batch')
        self.assertNotIn('concrete decision',read_receipt['result_json'] or '')
        restarted=EmberService(EmberStore(self.tmp.name),MockLLMClient())
        self.assertEqual(restarted.sidecar_call_tool('finish_memory_extraction_group',args,
                         recall_id='extract',tool_call_id='ack'),ack)
        self.assertEqual(Extraction(restarted).batch()['groups'],[])

    def test_partial_write_retains_other_slices_in_same_group_across_restart(self):
        first=self.source(1);second=self.source(2)
        part=self.grouped_sources(first,second)
        created=self.knowledge_write(first)
        with patch('ember.extraction.ask',side_effect=AssertionError('do not regroup remaining slices')):
            self.assertFalse(self.queue.discover(self.config))
        restarted=Extraction(EmberService(EmberStore(self.tmp.name),MockLLMClient()))
        remaining=restarted.batch()['groups'][0]
        self.assertEqual(remaining['group_id'],part['group_id'])
        self.assertEqual([r['slice_id'] for r in remaining['slices']],[second['slice_id']])
        self.assertEqual(self.receipts(first)[0]['result_id'],created['memory_id'])
        self.assertEqual(self.receipts(second),[])
        self.queue.finish({'group_id':part['group_id'],'source_slice_ids':[second['slice_id']]})
        self.assertEqual(self.queue.batch()['groups'],[])

    def test_event_and_promise_writes_auto_complete_even_when_discover_runs_before_return(self):
        for n,tool,kind in ((1,'write_event','daily'),(2,'write_promise','promise')):
            with self.subTest(tool=tool):
                source=self.source(n);self.discover();part=self.queue.batch()['groups'][0]
                self.service._live_turn={'session':self.service.get_or_create_session('current'),'turn':n}
                event={'summary':'a decision','core':'a concrete decision','kind':kind,'significance':8,
                       'focus':source['messages'][0]['content']}
                if kind=='promise':event['due']=None
                args={'event':event,'source_slice_ids':[source['slice_id']],
                      'recall_routes':[{'cue_type':'lexical','cue':'decision'}]}
                with patch('ember.source_sync.request_sync',side_effect=lambda _:self.queue.discover(self.config)):
                    result=self.service.sidecar_call_tool(tool,args)
                self.assertEqual(result['status'],'created',result)
                self.assertEqual(self.receipts(source)[0]['result_id'],result['event_id'])
                self.assertEqual(self.queue.finish({'group_id':part['group_id']})['state'],'completed')
                self.assertEqual(self.queue.batch()['groups'],[])

    def test_partial_event_write_does_not_withdraw_unused_sibling(self):
        first=self.source(1);second=self.source(2);part=self.grouped_sources(first,second)
        self.service._live_turn={'session':self.service.get_or_create_session('current'),'turn':1}
        args={'event':{'summary':'first','core':'only the first source','kind':'daily','significance':8,
                       'focus':first['messages'][0]['content']},'source_slice_ids':[first['slice_id']],
              'recall_routes':[{'cue_type':'lexical','cue':'first'}]}
        result=self.service.sidecar_call_tool('write_event',args)
        self.assertEqual(result['status'],'created',result)
        self.queue.discover(self.config)
        remaining=self.queue.batch()['groups'][0]
        self.assertEqual(remaining['group_id'],part['group_id'])
        self.assertEqual([r['slice_id'] for r in remaining['slices']],[second['slice_id']])

    def test_merge_knowledge_uses_explicit_new_sources_and_keeps_unused_sibling(self):
        old=self.source(99)
        knowledge=self.queue.knowledge
        mids=[knowledge.create('pattern',text,['decision'],[old['slice_id']])['memory_id']
              for text in ('original habit','another habit')]
        first=self.source(1);second=self.source(2);part=self.grouped_sources(first,second)
        from ember.knowledge_tools import dispatch
        result=dispatch(self.service,'review_memory_knowledge',{'group_id':part['group_id'],'items':[
            {'memory_id':mids[0],'action':'merge','merge_ids':[mids[1]],'content':'merged habit',
             'recall_cues':['decision'],'source_slice_ids':[first['slice_id']],'confidence':8}]})
        self.assertEqual(result['status'],'ok',result)
        self.assertEqual(self.receipts(first)[0]['result_id'],mids[0])
        self.assertEqual(self.receipts(first)[0]['action'],'merge')
        self.assertEqual(self.receipts(second),[])
        remaining=self.queue.batch()['groups'][0]
        self.assertEqual([r['slice_id'] for r in remaining['slices']],[second['slice_id']])

    def test_keep_and_failed_writes_do_not_consume_new_sources(self):
        old=self.source(99)
        mid=self.queue.knowledge.create('pattern','original habit',['decision'],[old['slice_id']])['memory_id']
        source=self.source(1);part=self.grouped_sources(source)
        from ember.knowledge_tools import dispatch
        kept=dispatch(self.service,'review_memory_knowledge',{'group_id':part['group_id'],
            'items':[{'memory_id':mid,'action':'keep','confidence':8}]})
        self.assertEqual(kept['status'],'ok',kept)
        failed=dispatch(self.service,'write_memory_knowledge',{'kind':'pattern','content':'invalid review',
            'recall_cues':['decision'],'source_slice_ids':[source['slice_id']],'confidence':4},
            {'session':self.service.get_or_create_session('current'),'turn':1})
        self.assertEqual(failed['status'],'failed')
        self.assertEqual(self.receipts(source),[])
        self.assertEqual(self.queue.batch()['groups'][0]['group_id'],part['group_id'])

    def test_skip_selected_slices_then_whole_group_is_idempotent(self):
        first=self.source(1);second=self.source(2);part=self.grouped_sources(first,second)
        args={'group_id':part['group_id'],'source_slice_ids':[first['slice_id']]}
        self.assertEqual(self.queue.finish(args)['state'],'pending')
        self.assertEqual(self.queue.finish(args)['state'],'pending')
        remaining=self.queue.batch()['groups'][0]
        self.assertEqual([r['slice_id'] for r in remaining['slices']],[second['slice_id']])
        done=self.queue.finish({'group_id':part['group_id']})
        self.assertEqual(done['state'],'completed')
        self.assertEqual(self.queue.finish(args)['status'],'ok')
        self.assertEqual(self.queue.batch()['groups'],[])
        self.assertFalse(self.queue.discover(self.config))
        with self.assertRaises(ValueError):self.queue.finish({'group_id':'never-existed'})

    def test_skip_withdrawn_group_succeeds_without_reading_unavailable_sources(self):
        source=self.source(1);self.discover();part=self.queue.batch()['groups'][0]
        self.source(1,'edited source')
        self.queue.batch()
        self.assertEqual(self.queue.finish({'group_id':part['group_id']})['status'],'ok')

    def test_recover_durable_result_after_queue_update_failure(self):
        source=self.source(1);part=self.grouped_sources(source)
        with patch('ember.extraction.Extraction.record_sources',side_effect=OSError('queue unavailable')):
            with self.assertRaises(OSError):self.knowledge_write(source)
        self.assertEqual(len(self.queue.knowledge.all()),1)
        restarted=Extraction(EmberService(EmberStore(self.tmp.name),MockLLMClient()))
        self.assertFalse(restarted.discover(self.config))
        self.assertEqual(restarted.batch()['groups'],[])
        self.assertEqual(self.queue.finish({'group_id':part['group_id']})['state'],'completed')

    def test_changed_source_version_is_not_consumed_by_old_result(self):
        source=self.source(1);self.grouped_sources(source);self.knowledge_write(source)
        edited=self.source(1,'new decision after correction')
        self.discover();part=self.queue.batch()['groups'][0]
        self.assertEqual(part['slices'][0]['messages'],edited['messages'])
        self.assertFalse(any(r['content_hash']==edited['content_hash'] for r in self.receipts(source)))

    def test_skip_remaining_material_does_not_reread_a_consumed_source_that_changed(self):
        first=self.source(1);second=self.source(2);part=self.grouped_sources(first,second)
        self.knowledge_write(first)
        self.source(1,'edited after successful extraction')
        # The old delivery still includes first; only unresolved sources are skipped.
        result=self.queue.finish({'group_id':part['group_id']})
        self.assertEqual(result['state'],'completed')
        self.assertEqual(self.receipts(second)[0]['action'],'skipped')

    def test_failed_seed_is_skipped_as_seed_and_neighbor_until_retry_deadline(self):
        first=self.source(1);second=self.source(2)
        self.cfg['extraction'].update(retry_initial_seconds=60,retry_max_seconds=300)
        with patch('ember.extraction.time.time',return_value=100), \
                patch('ember.extraction.ask',side_effect=ValueError('invalid group')):
            with self.assertRaises(ValueError):self.queue.discover(self.config)
        restarted=Extraction(self.service)
        def group(purpose,instruction,material,schema):
            ids=[r['slice_id'] for r in material['slices']]
            self.assertEqual(ids,[second['slice_id']])
            return {'groups':[{'description':'later source','slice_ids':ids}]}
        with patch('ember.extraction.time.time',return_value=130),patch('ember.extraction.ask',side_effect=group):
            self.assertTrue(restarted.discover(self.config))
        with patch('ember.extraction.time.time',return_value=159),patch('ember.extraction.ask') as model:
            self.assertFalse(restarted.discover(self.config))
            model.assert_not_called()
        with patch('ember.extraction.time.time',return_value=160),patch('ember.extraction.ask',return_value={
                'groups':[{'description':'recovered first','slice_ids':[first['slice_id']]}]}):
            self.assertTrue(restarted.discover(self.config))
        self.assertEqual(self.queue.batch()['grouping_errors'],0)

    def test_repeated_failure_backoff_grows_and_is_capped(self):
        self.source(1)
        self.cfg['extraction'].update(retry_initial_seconds=10,retry_max_seconds=25)
        now=100
        for count,delay in enumerate((10,20,25,25),1):
            with patch('ember.extraction.time.time',return_value=now), \
                    patch('ember.extraction.ask',side_effect=OSError('PRIVATE failure body')):
                with self.assertRaises(OSError):self.queue.discover(self.config)
            with self.queue.db() as conn:
                row=conn.execute('SELECT * FROM grouping_retries').fetchone()
            self.assertEqual(row['failures'],count)
            self.assertEqual(row['retry_at'],now+delay)
            self.assertEqual(row['error'],'OSError')
            now+=delay

    def test_new_source_version_does_not_inherit_old_backoff(self):
        source=self.source(1)
        with patch('ember.extraction.time.time',return_value=100),patch('ember.extraction.ask',side_effect=ValueError):
            with self.assertRaises(ValueError):self.queue.discover(self.config)
        self.source(1,'corrected source')
        with patch('ember.extraction.time.time',return_value=101):
            model=self.discover()
        self.assertEqual(model.call_count,1)
        self.assertEqual(self.queue.batch()['grouping_errors'],0)

    def test_consumed_failed_seed_clears_outstanding_failure(self):
        source=self.source(1)
        with patch('ember.extraction.ask',side_effect=ValueError):
            with self.assertRaises(ValueError):self.queue.discover(self.config)
        self.knowledge_write(source)
        with patch('ember.extraction.ask') as model:
            self.assertFalse(self.queue.discover(self.config))
            model.assert_not_called()
        self.assertEqual(self.queue.batch()['grouping_errors'],0)
