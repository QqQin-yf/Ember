import json
from pathlib import Path
import tempfile
import unittest

from ember.memory_lifecycle import Lifecycle
from ember.review_aggregation import consolidate
from ember.knowledge import KnowledgeStore
from ember.turn_slices import TurnSliceStore
from ember.service import EmberService
from ember.store import EmberStore
from ember.llm import MockLLMClient
from tests.legacy_sleep_fixture import SleepKnowledge
from ember.memory_maintenance import catalog, knowledge_reviews


class ReviewAggregationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.life=Lifecycle(self.root);self.store=KnowledgeStore(self.root)
        self.service=EmberService(EmberStore(str(self.root)),MockLLMClient())
        self.rows={}
        for index,name in enumerate('ABCD'):
            original=TurnSliceStore(str(self.root)).save(name,[
                {'message_id':2*index+1,'role':'user','content':name+' lunch experience'},
                {'message_id':2*index+2,'role':'assistant','content':name+' response'}])
            self.rows[name]={'id':original['slice_id'],'kind':'slice',
                'document':{'messages':original['messages']},'sources':self.store.sources([original['slice_id']])}

    def submit(self,names,topic='Lunch choice',kind='habit',**extra):
        items=[self.rows[n] for n in names]
        return self.life.propose(kind,[s for row in items for s in row['sources']],'Grouping evidence',
            dict(direction=topic,items=items,**extra))

    def test_different_slice_combinations_accumulate_one_pending_direction(self):
        gid=self.submit('AB')
        self.assertEqual(gid,self.submit('BC'))
        self.assertEqual(gid,self.submit('ACD'))
        groups=self.store.groups()
        self.assertEqual(len(groups),1)
        self.assertEqual(len(groups[0]['sources']),4)
        self.assertEqual(len(groups[0]['material']['items']),4)

    def test_processed_group_archives_ordinary_supplements_without_reopening(self):
        gid=self.submit('AB');self.store.finish_group(gid,'reviewed')
        self.assertEqual(gid,self.submit('BCD'))
        self.assertEqual(self.store.groups(),[])
        self.assertEqual(len(self.store.groups(False)[0]['sources']),4)

    def test_replaying_original_legacy_submission_does_not_shrink_expanded_group(self):
        gid=self.submit('AB',topic='')
        self.submit('BC',topic='',group_id=gid)
        self.store.finish_group(gid,'reviewed')
        self.assertEqual(self.submit('AB',topic=''),gid)
        self.assertEqual(self.store.groups(),[])
        self.assertEqual(len(self.store.groups(False)[0]['sources']),3)

    def test_raw_review_categories_and_explicit_group_reference_share_direction(self):
        gid=self.submit('AB',topic='  Lunch CHOICE ',kind='habit')
        self.assertEqual(gid,self.submit('BC',topic='lunch choice',kind='pattern'))
        self.assertEqual(gid,self.submit('CD',topic='Equivalent wording',kind='theme',group_id=gid))
        self.assertEqual(gid,self.submit('AB',topic='lunch choice',kind='habit'))
        self.assertEqual(len(self.store.groups()),1)

    def test_distinct_directions_are_not_merged_just_because_sources_overlap(self):
        first=self.submit('AB',topic='Lunch choice')
        second=self.submit('AB',topic='Attitude toward spending')
        self.assertNotEqual(first,second)
        self.assertEqual(len(self.store.groups()),2)

    def test_change_reopens_once_but_repeated_detection_does_not(self):
        gid=self.submit('AB');self.store.finish_group(gid,'reviewed')
        evidence=[{'id':self.rows[n]['id'],'quote':n+' lunch experience'} for n in 'BC']
        self.assertEqual(self.submit('BC',group_id=gid,change_evidence=evidence),gid)
        self.assertEqual(len(self.store.groups()),1)
        self.store.finish_group(gid,'reviewed')
        self.submit('BC',group_id=gid,change_evidence=evidence)
        self.assertEqual(self.store.groups(),[])

    def test_sleep_material_is_frozen_and_supplements_are_flushed_after_finish(self):
        gid=self.submit('AB');stage=SleepKnowledge(self.service)
        identity={'sleep_id':'active','session_id':'chat'}
        before=stage.start(identity)
        self.submit('BC')
        self.assertEqual(stage.start(identity)['groups'],before['groups'])
        self.assertEqual(len(self.store.groups()[0]['sources']),2)
        self.store.finish_group(gid,'reviewed')
        result=stage.finish(dict(identity,state='completed'))
        self.assertEqual(result['state'],'completed')
        self.assertEqual(self.store.groups(),[])
        self.assertEqual(len(self.store.groups(False)[0]['sources']),3)

    def test_cancelled_sleep_keeps_one_expanded_pending_task(self):
        self.submit('AB');stage=SleepKnowledge(self.service)
        identity={'sleep_id':'cancel','session_id':'chat'}
        stage.start(identity);self.submit('BC')
        stage.finish(dict(identity,state='stopped'))
        self.assertEqual(len(self.store.groups()),1)
        self.assertEqual(len(self.store.groups()[0]['sources']),3)

    def test_change_during_sleep_is_next_sleep_work_not_replayed_old_work(self):
        gid=self.submit('AB');stage=SleepKnowledge(self.service)
        identity={'sleep_id':'first','session_id':'chat'}
        stage.start(identity)
        evidence=[{'id':self.rows[n]['id'],'quote':n+' lunch experience'} for n in 'BC']
        self.submit('BC',group_id=gid,change_evidence=evidence)
        self.store.finish_group(gid,'reviewed')
        self.assertEqual(stage.finish(dict(identity,state='completed'))['remaining'],0)
        self.assertEqual(stage.start(identity)['remaining'],0)
        self.assertEqual(stage.start({'sleep_id':'next','session_id':'chat'})['remaining'],1)

    def knowledge(self):
        ids=[]
        for name in 'ABC':
            ids.append(self.store.create('pattern','Knowledge '+name,['cue-'+name],
                [self.rows[name]['id']])['memory_id'])
        return ids

    def test_connected_similarity_pairs_are_one_review_not_three(self):
        ids=self.knowledge();rows=[r for r in catalog(self.service) if r['kind']=='knowledge']
        knowledge_reviews(self.service,rows,{mid:[1.,0.] for mid in ids})
        groups=self.store.groups()
        self.assertEqual(len(groups),1)
        self.assertEqual(set(groups[0]['material']['memory_ids']),set(ids))

    def test_sleep_consolidates_legacy_overlapping_pair_tasks(self):
        ids=self.knowledge();rows={r['id']:r for r in catalog(self.service) if r['kind']=='knowledge'}
        for index,pair in enumerate((ids[:2],ids[1:])):
            gid='legacy-'+str(index);items=[rows[mid] for mid in pair]
            with self.store.db() as db:
                db.execute('INSERT INTO memory_groups VALUES (?,?,?,?,?,?)',(gid,'default','knowledge_review',
                    json.dumps([s for row in items for s in row['sources']]),'legacy pair','pending'))
            with self.life.db() as db:
                db.execute('INSERT INTO review_materials VALUES (?,?)',(gid,json.dumps({'memory_ids':pair,'items':items})))
        stage=SleepKnowledge(self.service)
        result=stage.start({'sleep_id':'legacy','session_id':'chat'})
        self.assertEqual(result['remaining'],1)
        self.assertEqual(set(result['groups'][0]['material']['memory_ids']),set(ids))

    def test_stale_pre_sleep_versions_do_not_reopen_just_reviewed_knowledge(self):
        ids=self.knowledge()[:2]
        rows=[r for r in catalog(self.service) if r['id'] in ids]
        knowledge_reviews(self.service,rows,{mid:[1.,0.] for mid in ids})
        gid=self.store.groups()[0]['group_id']
        stage=SleepKnowledge(self.service);identity={'sleep_id':'versions','session_id':'chat'}
        stage.start(identity)
        knowledge_reviews(self.service,rows,{mid:[1.,0.] for mid in ids})
        with self.store.db() as db:
            db.execute("UPDATE knowledge SET content_hash='new-reviewed-version' WHERE memory_id=?",(ids[0],))
        self.store.finish_group(gid,'reviewed')
        stage.finish(dict(identity,state='completed'))
        self.assertEqual(self.store.groups(),[])
        consolidate(self.life)
        self.assertEqual(self.store.groups(),[])


if __name__=='__main__':
    unittest.main()
