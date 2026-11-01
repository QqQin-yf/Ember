from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ember.llm import MockLLMClient
from ember.models import Event
from ember.service import EmberService
from ember.store import EmberStore
from ember.timeline import Timeline, TimelineWorker
from ember.timeline_work import request, ready


class TimelineWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=EmberService(EmberStore(self.tmp.name),MockLLMClient())
        self.worker=TimelineWorker(SimpleNamespace(names=['one'],service=lambda _:self.service))

    def event(self,eid='r',kind='relationship'):
        event=Event(eid,'v','x','x','2026-01-01','2026-01-01',core=eid,kind=kind)
        self.service.store.write_event(event)
        return event

    def test_idle_ticks_do_not_scan_or_synchronize_and_pin_changes_wake(self):
        self.event()
        with patch('ember.timeline.prose',return_value='short'):
            self.assertTrue(self.worker.process_one('one'))
        with patch.object(self.service.store,'all_events',side_effect=AssertionError('idle scan')), \
                patch.object(Timeline,'synchronize',side_effect=AssertionError('idle sync')):
            for _ in range(3):
                self.assertFalse(self.worker.process_one('one'))
        self.service.pin_store.add('r')
        with patch('ember.timeline.prose',side_effect=AssertionError('no recompression')):
            self.assertTrue(self.worker.process_one('one'))
            self.assertFalse(self.worker.process_one('one'))
        self.service.pin_store.add('r')
        self.assertFalse(self.worker.process_one('one'))
        self.service.pin_store.remove('r')
        self.assertTrue(self.worker.process_one('one'))

    def test_retry_waits_for_deadline_and_survives_restart(self):
        self.event()
        with patch('ember.timeline.time.time',return_value=1000), \
                patch('ember.timeline.prose',side_effect=OSError('offline')):
            self.worker.process_one('one')
        self.assertIsNone(ready(self.tmp.name,now=1000))
        self.assertIsNotNone(ready(self.tmp.name,now=100000))
        restarted=TimelineWorker(self.worker.registry)
        with patch('ember.timeline.prose',return_value='recovered'):
            self.assertTrue(restarted.process_one('one'))
        self.assertIsNone(ready(self.tmp.name))

    def test_new_write_during_processing_is_not_lost(self):
        self.event()
        with patch.object(Timeline,'prepare',side_effect=lambda:request(self.tmp.name)), \
                patch.object(Timeline,'link',return_value=False):
            self.worker.process_one('one')
        self.assertIsNotNone(ready(self.tmp.name))

    def test_ordinary_events_do_not_wake_and_kind_change_does(self):
        self.worker.process_one('one')
        event=self.event('daily','daily')
        self.assertIsNone(ready(self.tmp.name))
        event.kind='relationship';self.service.store.write_event(event)
        self.assertIsNotNone(ready(self.tmp.name))
        with patch('ember.timeline.prose',return_value='short'):
            self.worker.process_one('one')
        event.kind='daily';self.service.store.write_event(event)
        self.assertTrue(self.worker.process_one('one'))

    def test_startup_recovers_missing_wakeup_after_durable_event(self):
        self.event()
        from ember.timeline_work import finish
        finish(self.tmp.name,ready(self.tmp.name))
        with patch('ember.timeline.prose',return_value='recovered') as model:
            self.assertTrue(self.worker.process_one('one'))
        self.assertEqual(model.call_count,1)
