from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch

from ember.llm import MockLLMClient
from ember.memory_lifecycle import Lifecycle
from ember.models import Event, RecallRoute
from ember.promises import Promises
from ember.service import EmberService
from ember.store import EmberStore
from tests.test_sidecar import write_args


class PromiseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=EmberService(EmberStore(self.tmp.name),MockLLMClient())
        self.promises=Promises(self.service)
        self.span={'session_id':'s','message_ids':['71','75']}

    def event(self,eid='p',score=7,due=None):
        ev=Event(eid,'v','x','x','2026-01-01','2026-01-01',summary='看海的约定',core='我们约好去看海。',
                 kind='promise',significance=score,due=due)
        self.service.store.write_event(ev)
        return ev

    def test_only_exposed_promises_can_be_processed_and_log_is_mandatory(self):
        ev=self.event()
        self.assertEqual(self.promises.pending(),[])
        with self.assertRaises(ValueError):
            self.promises.process({'event_id':'p','log':'还没有做'})
        self.promises.enter('p','cue',self.span)
        self.promises.enter('p','cue',self.span)
        self.assertEqual(len(self.promises.pending()[0]['contexts']),1)
        with self.assertRaises(ValueError):
            self.promises.process({'event_id':'p','log':' '})
        self.promises.process({'event_id':'p','log':'又提到了，但还没做'})
        self.assertEqual(ev.status,'open')
        self.assertEqual(ev.log[0]['spans'],[self.span])
        self.assertEqual(self.promises.pending(),[])
        self.promises.enter('p','recall',dict(self.span,message_ids=['80']))
        self.promises.process({'event_id':'p','log':'决定不去了','status':'dropped'})
        self.assertEqual(len(ev.log),2)
        self.assertFalse(Lifecycle(self.tmp.name).eligible('p'))

    def test_reschedule_removes_overdue_and_high_score_done_stays_recallable(self):
        ev=self.event(score=8,due='2026-01-01T09:00:00+08:00')
        self.assertEqual(self.promises.overdue()[0].event_id,'p')
        self.promises.enter('p','briefing',self.span)
        self.promises.process({'event_id':'p','log':'改到明年','due':'2027-01-01T09:00:00+08:00'})
        self.assertEqual(self.promises.overdue(),[])
        self.promises.enter('p','cue',dict(self.span,message_ids=['90']))
        self.promises.process({'event_id':'p','log':'已经去了','status':'done'})
        self.assertTrue(Lifecycle(self.tmp.name).eligible('p'))

    def test_local_midnight_sweep_and_restart_catchup_are_idempotent(self):
        ev=self.event(due='2026-11-01T12:00:00+08:00')
        eternal=self.event('eternal')
        for now in ['2026-11-08T00:00:00+08:00','2026-11-08T23:59:00+08:00']:
            self.promises.sweep(datetime.fromisoformat(now))
            self.assertEqual(ev.status,'open')
        self.promises.sweep(datetime.fromisoformat('2026-11-09T00:00:00+08:00'))
        self.assertEqual(ev.status,'lapsed')
        self.assertEqual(len(ev.log),1)
        self.promises.sweep(datetime.fromisoformat('2026-11-09T09:00:00+08:00'))
        self.assertEqual(len(ev.log),1)
        self.assertEqual(eternal.status,'open')

    def test_context_entry_is_a_replayable_snapshot_effect(self):
        self.event(score=8)
        self.service.store.write_route(RecallRoute('r','p','lexical','lexical','看海'))
        result=self.service.hook_turn_start('s','看海',user_message_id=71,recall_id='recall')
        self.assertTrue(result['injected'])
        first=self.promises.pending()
        self.assertEqual(len(first),1)
        self.assertEqual(first[0]['contexts'][0]['span']['message_ids'],['71'])
        self.service.hook_turn_start('s','看海',user_message_id=71,recall_id='recall')
        self.assertEqual(self.promises.pending(),first)

    def test_uncommitted_recall_does_not_expose_promise(self):
        self.event(score=8)
        self.service.store.write_route(RecallRoute('r','p','lexical','lexical','看海'))
        with patch.object(self.service.generations,'commit_snapshot',side_effect=OSError('fixture')):
            self.service.hook_turn_start('s','看海',user_message_id=71,recall_id='recall')
        self.assertEqual(self.promises.pending(),[])

    def test_ten_without_due_is_pinned_on_real_write(self):
        with patch.object(self.service,'_fetched_corpus',return_value=[]):
            self.service.hook_turn_start('s','今天我们决定把传播模型改成 Route Search。',user_message_id=71,recall_id='r')
        args=write_args();args['event'].update(kind='promise',significance=10,due=None)
        result=self.service.sidecar_call_tool('write_promise',args,recall_id='r',tool_call_id='t')
        self.assertEqual(result['status'],'created',result)
        self.assertTrue(self.service.pin_store.is_pinned(result['event_id']))

    def test_log_commit_recovers_before_receipt_ack(self):
        self.event()
        self.promises.enter('p','cue',self.span)
        self.promises.process({'event_id':'p','log':'未变'})
        with self.promises.db() as conn:
            conn.execute('UPDATE exposures SET processed=0')
        restarted=EmberService(EmberStore(self.tmp.name),MockLLMClient())
        self.assertEqual(Promises(restarted).pending(),[])
        self.assertEqual(len(restarted.store.get_event('p').log),1)
