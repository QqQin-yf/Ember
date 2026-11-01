import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ember.event_slices import EventSliceLinks
from ember.generations import fingerprint
from ember.maintenance_client import MaintenanceConfig, MaintenanceModelError
from ember.maintenance_materials import prepare, stage_one, stage_two, event_document
from ember.slice_maintenance import MaintenanceQueue, SliceMaintenanceWorker, validate_decisions
from ember.slice_recall import SliceRecall
from ember.slice_embedding import SliceEmbeddingConfig
from ember.turn_slices import TurnSliceStore


class SliceMaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = TurnSliceStore(str(self.root))
        self.old = self.save('old', 1, 'old detail')
        self.first = self.save('new', 11, 'NEW FIRST SECRET')
        self.second = self.save('new', 13, 'NEW SECOND SECRET')
        self.event = {'event_id': 'N', 'version_id': 'v1', 'summary': 'new understanding',
                      'key_quotes': 'RAW QUOTE SECRET'}
        self.old_event = {'event_id': 'O', 'version_id': 'v1', 'summary': 'old understanding'}
        self.write_events()
        self.links = EventSliceLinks(self.root)
        self.links.register(self.ref('N', 'new', [(1, 11), (2, 13)]))
        self.links.register(self.ref('O', 'old', [(1, 1)]))
        self.queue = MaintenanceQueue(self.root)
        self.registry = SimpleNamespace(names=['default'], dir_of=lambda name: str(self.root))
        self.worker = SliceMaintenanceWorker(self.registry, self.root / 'missing.json')
        self.pool('t1', 1, 11, [self.old, self.first])
        self.pool('t2', 2, 13, [self.old])
        self.root.joinpath('turn_ledger.jsonl').write_text('\n'.join(json.dumps({
            'ember_session': 'new', 'ember_turn': turn,
            'user_message_id': str(user), 'assistant_message_id': str(user+1)})
            for turn, user in [(1, 11), (2, 13)]), encoding='utf-8')

    def save(self, session, user, text):
        return self.store.save(session, [
            {'message_id': user, 'role': 'user', 'content': text},
            {'message_id': user+1, 'role': 'assistant', 'content': 'reply'}])

    def write_events(self):
        self.root.joinpath('events.jsonl').write_text(
            '\n'.join(json.dumps(e) for e in [self.event, self.old_event]), encoding='utf-8')

    def ref(self, eid, sid, turns):
        return {'event_id': eid, 'version_id': 'v1', 'source_session_id': sid,
            'source_evidence': 'frozen_messages', 'source_turn_spans': [[1, len(turns)]],
            'turn_messages': [{'turn': t, 'user_message_id': str(u),
                               'assistant_message_id': str(u+1)} for t, u in turns]}

    def pool(self, trace, turn, user, sources, operation=False):
        candidates = [{'slice_id': s['slice_id'], 'content_hash': s['content_hash'],
            'session_id': s['session_id'], 'user_message_id': s['user_message_id'],
            'assistant_message_id': s['assistant_message_id'],
            'span_memberships': self.links.for_slice(s['slice_id'])} for s in sources]
        meta = {'trace_id': trace, 'session_id': 'new', 'turn': turn,
                'anchor_message_id': str(user), 'status': 'ranked', 'candidate_pool': candidates}
        with SliceRecall(self.root).db() as db:
            db.execute('INSERT INTO queries VALUES (?,?,?,?,?,?)',
                (trace, 'default', json.dumps(meta), None, '[]', 'now'))

    def material(self):
        return prepare(self.root, 'default', 'N')

    def config(self):
        return patch('ember.slice_maintenance.MaintenanceConfig.load',
                     return_value=MaintenanceConfig('https://example.invalid/v1', 'test', 'test'))

    def event_decision(self, verdict='related'):
        return [{'candidate_id': self.old['slice_id'], 'verdict': verdict,
            'relationship': 'relevant detail', 'old_evidence': 'old detail',
            'event_evidence': 'new understanding'}]

    def turn_decision(self, action='link'):
        return [{'candidate_id': self.old['slice_id'], 'turns': [
            {'turn': 1, 'verdict': 'related', 'relationship': 'same affair followup',
             'old_evidence': 'old detail', 'new_evidence': 'NEW FIRST SECRET',
             'action': action, 'target_event_id': 'O'},
            {'turn': 2, 'verdict': 'unrelated'}]}]

    def start(self):
        self.queue.enqueue(self.event)
        self.worker._last_discover = float('inf')

    def test_material_uses_only_recorded_pools_and_excludes_new_span(self):
        self.pool('preview', 99, 99, [self.second])
        m = self.material()
        self.assertEqual(len(m['candidates']), 1)
        self.assertEqual([t['turn'] for t in m['candidates'][0]['triggers']], [1, 2])
        self.assertEqual(m['excluded_self_span'][0]['slice_id'], self.first['slice_id'])
        self.assertEqual(m['missing_candidate_turns'], [])
        # A preview with plausible identity still has no completed turn evidence.
        self.root.joinpath('turn_ledger.jsonl').unlink()
        self.assertEqual(self.material()['candidates'], [])

    def test_phase_one_has_no_new_raw_or_quote_and_phase_two_has_only_triggers(self):
        m = self.material()
        one = json.dumps(stage_one(self.root, m, m['candidates']))
        self.assertIn('old detail', one)
        for secret in ['NEW FIRST SECRET', 'NEW SECOND SECRET', 'RAW QUOTE SECRET']:
            self.assertNotIn(secret, one)
        m['candidates'][0]['triggers'] = m['candidates'][0]['triggers'][:1]
        two = json.dumps(stage_two(self.root, m, m['candidates']))
        self.assertIn('NEW FIRST SECRET', two)
        self.assertNotIn('NEW SECOND SECRET', two)

    def test_missing_config_waits_without_calling_model(self):
        self.start()
        with patch('ember.slice_maintenance.MaintenanceClient.judge') as judge:
            self.worker.process_one()
        judge.assert_not_called()
        self.assertEqual(self.queue.statuses()[0]['state'], 'waiting_config')

    def test_phase_two_waits_for_missing_trigger_without_repeating_phase_one(self):
        self.start()
        with self.config(), patch('ember.slice_maintenance.MaintenanceClient.judge',
                                  return_value=self.event_decision('unrelated')) as judge:
            self.worker.process_one()
            with self.store._db() as db:
                db.execute('DELETE FROM turn_slices WHERE slice_id=?', (self.second['slice_id'],))
            self.worker.process_one()
        self.assertEqual(judge.call_count, 1)
        self.assertEqual(self.queue.statuses()[0]['state'], 'waiting_source')
        with self.queue.db() as db:
            saved = json.loads(db.execute('SELECT data_json FROM slice_maintenance_jobs').fetchone()[0])
        self.assertIn(self.old['slice_id'], saved['stage_one'])
        self.assertEqual(saved['stage_two'], {})

    def test_normal_event_write_enqueues_without_sleep_or_model_call(self):
        from ember.service import EmberService
        from ember.store import EmberStore
        from ember.llm import MockLLMClient
        from tests.test_generations import TEXT
        from tests.test_sidecar import write_args
        # Use another root to exercise the real public write path.
        root = self.root / 'write-test'
        service = EmberService(EmberStore(str(root)), MockLLMClient())
        with patch('ember.slice_maintenance.MaintenanceClient.judge') as judge:
            service.hook_turn_start('s', TEXT, user_message_id=71, recall_id='maintenance-write')
            result = service.sidecar_call_tool('write_event', write_args(),
                recall_id='maintenance-write', tool_call_id='create')
        judge.assert_not_called()
        self.assertEqual(result['status'], 'created')
        from ember.source_sync import db
        with db(root) as conn:
            job=conn.execute('SELECT requested,completed FROM sync').fetchone()
        self.assertEqual(tuple(job),(1,0))
        self.assertEqual(MaintenanceQueue(root).statuses(),[])
        self.assertFalse(hasattr(service, "link_pending_add"))

    def test_discovery_recovers_missing_enqueue_idempotently(self):
        self.worker.discover()
        self.worker.discover()
        self.assertEqual(len(self.queue.statuses()), 2)

    def test_phase_one_related_persists_and_skips_phase_two_after_restart(self):
        self.start()
        with self.config(), patch('ember.slice_maintenance.MaintenanceClient.judge',
                                  return_value=self.event_decision()) as judge:
            self.worker.process_one()
            replacement = SliceMaintenanceWorker(self.registry)
            replacement._last_discover = float('inf')
            replacement.process_one()
        self.assertEqual(judge.call_count, 1)
        self.assertEqual(self.queue.statuses()[0]['state'], 'done')
        relation = self.queue.relations()[0]
        self.assertEqual(relation['scope'], 'event')
        self.assertIsNone(relation['new_slice_id'])
        # Changes revoke derived relations rather than applying stale judgments.
        self.event['summary'] = 'changed'
        self.write_events()
        self.assertEqual(self.queue.relations(), [])

    def test_second_phase_distinguishes_turns_and_adds_source_without_moving_span(self):
        self.start()
        with self.config(), patch('ember.slice_maintenance.MaintenanceClient.judge',
                side_effect=[self.event_decision('unrelated'), self.turn_decision('add_source')]):
            self.worker.process_one()
            self.worker.process_one()
            self.worker.process_one()
        self.assertEqual(self.queue.statuses()[0]['state'], 'done')
        relations = self.queue.relations()
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]['new_slice_id'], self.first['slice_id'])
        memberships = self.links.for_slice(self.first['slice_id'])
        self.assertEqual({m['membership_kind'] for m in memberships}, {'span', 'added_source'})
        self.assertEqual([m['event_id'] for m in memberships if m['membership_kind']=='added_source'], ['O'])
        self.assertEqual([m['event_id'] for m in self.links.for_slice(self.second['slice_id'])], ['N'])
        self.assertEqual(self.store.get(self.first['slice_id'])['content_hash'], self.first['content_hash'])

    def test_both_unrelated_records_observation_only(self):
        self.start()
        decision = self.turn_decision()
        decision[0]['turns'] = [{'turn': n, 'verdict': 'unrelated'} for n in (1, 2)]
        with self.config(), patch('ember.slice_maintenance.MaintenanceClient.judge',
                side_effect=[self.event_decision('unrelated'), decision]):
            for _ in range(3):
                self.worker.process_one()
        self.assertFalse(self.queue.relations())
        with self.queue.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM slice_maintenance_observations').fetchone()[0], 1)
        self.assertEqual(len(self.material()['candidates']), 1)

    def test_invalid_evidence_and_forged_destination_rejected(self):
        m = self.material()
        payload = stage_one(self.root, m, m['candidates'])
        bad = self.event_decision();bad[0]['old_evidence'] = 'invented'
        with self.assertRaises(MaintenanceModelError):
            validate_decisions('event', payload, bad)
        bad = self.event_decision()
        bad[0]['old_evidence'] = 'old detailreply'
        with self.assertRaises(MaintenanceModelError):
            validate_decisions('event', payload, bad)
        bad[0]['old_evidence'] = 'old detail\nreply'
        with self.assertRaises(MaintenanceModelError):
            validate_decisions('event', payload, bad)
        payload['new_event']['core'] = 'separate field'
        bad = self.event_decision()
        bad[0]['event_evidence'] = 'new understanding\nseparate field'
        with self.assertRaises(MaintenanceModelError):
            validate_decisions('event', payload, bad)
        payload = stage_two(self.root, m, m['candidates'])
        bad = self.turn_decision('add_source');bad[0]['turns'][0]['target_event_id'] = 'invented'
        with self.assertRaises(MaintenanceModelError):
            validate_decisions('turn', payload, bad)

    def test_lease_prevents_stale_worker_commit(self):
        self.start()
        first = self.queue.claim()
        with self.queue.db() as db:
            db.execute('UPDATE slice_maintenance_jobs SET lease_until=0')
        second = self.queue.claim()
        self.queue.save(first, 'done', {'stale': True})
        self.assertEqual(self.queue.statuses()[0]['state'], 'working')
        self.queue.save(second, 'done', {})
        self.assertEqual(self.queue.statuses()[0]['state'], 'done')

    def test_recall_reads_maintained_links_and_source_membership(self):
        self.test_second_phase_distinguishes_turns_and_adds_source_without_moving_span()
        config = SliceEmbeddingConfig('test', 'https://example.invalid/v1', 'test')
        self.store.save_embedding(self.first, config.space, [1., 0.])
        with patch('ember.slice_recall.SliceEmbeddingConfig.load', return_value=config), \
                patch('ember.slice_recall.SliceEmbeddingClient.embed', return_value=[1., 0.]):
            recall = SliceRecall(self.root)
            result = recall.match('current',purpose='search')
        candidate = recall.trace(result['trace_id'])['candidate_pool'][0]
        self.assertEqual(candidate['maintenance_relations'][0]['scope'], 'turn')
        self.assertEqual(candidate['source_memberships'][0]['event_id'], 'O')


if __name__ == '__main__':
    unittest.main()
