"""Frozen recall responses, exact 24h expiry and crash-safe effect recovery."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from ember.generations import SNAPSHOT_TTL_SECONDS, fingerprint
from ember.instances import InstanceRegistry
from ember.llm import MockLLMClient
from ember.models import Event, RecallRoute
from ember.recall_ledger import RecallLedger
from tests.legacy_event_fixture import LegacyEventService as EmberService
from ember.store import EmberStore
from ember.turn_ledger import TurnLedger

CANARY = "original-injected-body-canary"


class RecallSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.svc.store.write_event(Event(event_id="A", version_id="v1",
            source_ref="conversation/session-old", raw_ref="old", event_time="2020-01-01",
            ingest_time="2020-01-01", summary="A summary", core=CANARY))
        self.svc.store.write_route(RecallRoute(route_id="route-A", event_id="A",
            matcher="lexical", cue_type="lexical", cue="beacon"))
        self.svc.turn_spans.fill("A", [(1, 1)], filled_by="test")
        self.svc.event_sources.capture("A", "v1", "old", [(1, 1)], [
            {"turn": 1, "role": "user", "_source_session_id": "old", "_source_message_id": "30"},
            {"turn": 1, "role": "assistant", "_source_session_id": "old", "_source_message_id": "31"}])

    def start(self, rid="r1", **kwargs):
        return self.svc.hook_turn_start("s", "remember beacon", user_message_id=101,
                                        recall_id=rid, **kwargs)

    def test_same_id_replays_exact_result_without_recalling_or_refreshing_counts(self):
        original = self.svc.run_recall_pipeline
        with patch.object(self.svc, "run_recall_pipeline", wraps=original) as pipeline:
            first = self.start()
            count = dict(self.svc.recall_ledger.event_recall_count)
            clock = dict(self.svc.recall_ledger.last_injected)
            second = self.start()
        self.assertEqual(pipeline.call_count, 1)
        self.assertEqual(second, first)
        self.assertIn(CANARY, first["injected_text"])
        self.assertEqual(self.svc.sessions["s"].turn_counter, 1)
        self.assertEqual(self.svc.recall_ledger.event_recall_count, count)
        self.assertEqual(self.svc.recall_ledger.last_injected, clock)
        self.assertEqual(len(self.svc.turn_ledger.all_turns), 1)

    def test_metadata_query_has_no_body_and_preserves_source_and_version(self):
        first = self.start()
        result = self.svc.sidecar_recall_snapshot("r1")
        self.assertEqual(result["status"], "ready")
        self.assertNotIn(CANARY, json.dumps(result))
        self.assertNotIn("injected_text", json.dumps(result))
        self.assertEqual(result["snapshot"]["recall_refs"], first["recall_refs"])
        self.assertEqual(result["source_candidates"], first["recall_refs"]["events"])
        self.assertEqual(result["sources_revision"], 0)
        self.assertEqual(self.svc.sidecar_recall_snapshot("r1", "response")["response"], first)

    def test_later_revise_pin_and_source_changes_do_not_reconstruct_old_snapshot(self):
        first = self.start()
        event = self.svc.store.get_event("A")
        event.core = "newly revised content"
        event.version_id = "new-version"
        self.svc.store.write_event(event)
        self.svc.pin_store.add("A")
        self.svc.turn_ledger.record_reply("old", 1, 999)
        self.svc.turn_spans.fill("A", [(9, 9)], filled_by="changed")
        replay = self.start()
        self.assertEqual(replay, first)
        self.assertEqual(replay["recall_refs"]["events"][0]["version_id"], "v1")
        self.assertEqual(replay["recall_refs"]["events"][0]["turn_messages"][0]["assistant_message_id"], "31")

    def test_restart_replays_response_but_never_reopens_generation(self):
        first = self.start()
        counts = dict(self.svc.recall_ledger.event_recall_count)
        again = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        with patch.object(again, "run_recall_pipeline", side_effect=AssertionError("must not rerun")):
            second = again.hook_turn_start("s", "remember beacon", user_message_id=101, recall_id="r1")
        self.assertEqual(second, first)
        self.assertEqual(again.recall_ledger.event_recall_count, counts)
        self.assertIsNone(again._live_turn)
        self.assertEqual(again.sidecar_call_tool("pin_memory", {"event_id": "A"}, recall_id="r1", tool_call_id="new")["code"], "stale_generation")

    def test_expiry_at_exact_24h_deletes_body_but_not_refs_memory_or_identity(self):
        with patch("ember.generations.time.time", return_value=1000):
            first = self.start()
        path = Path(self.svc.generations.path)
        self.assertIn(CANARY.encode(), path.read_bytes())
        event_bytes = (Path(self.tmp.name) / "events.jsonl").read_bytes()
        with patch("ember.generations.time.time", return_value=1000 + SNAPSHOT_TTL_SECONDS - 1):
            self.assertEqual(self.svc.sidecar_recall_snapshot("r1", "response")["response"], first)
            self.assertEqual(self.svc.generations.snapshot("r1")["expires_at"], 1000 + SNAPSHOT_TTL_SECONDS)
        with patch("ember.generations.time.time", return_value=1000 + SNAPSHOT_TTL_SECONDS):
            self.assertEqual(self.svc.sidecar_recall_snapshot("r1", "response")["code"], "response_expired")
            self.assertEqual(self.start()["code"], "response_expired")
            refs = self.svc.sidecar_recall_snapshot("r1")
            self.assertEqual(refs["status"], "ready")
            self.assertFalse(refs["response_available"])
            self.assertEqual(refs["snapshot"]["recall_refs"], first["recall_refs"])
        self.assertNotIn(CANARY.encode(), path.read_bytes())
        self.assertEqual((Path(self.tmp.name) / "events.jsonl").read_bytes(), event_bytes)
        self.assertEqual(self.svc.store.get_event("A").core, CANARY)
        self.assertEqual(self.svc.sessions["s"].turn_counter, 1)
        self.assertIsNotNone(self.svc.generations.generation("r1"))

    def test_startup_purges_expired_body_and_keeps_tombstone(self):
        with patch("ember.generations.time.time", return_value=1000):
            self.start()
        with patch("ember.generations.time.time", return_value=1000 + SNAPSHOT_TTL_SECONDS):
            again = EmberService(EmberStore(self.tmp.name), MockLLMClient())
            self.assertEqual(again.sidecar_recall_snapshot("r1", "response")["code"], "response_expired")
            self.assertEqual(again.hook_turn_start("s", "remember beacon", user_message_id=101, recall_id="r1")["code"], "response_expired")
        self.assertIsNone(again.generations.snapshot("r1")["response_json"])
        self.assertEqual(again.generations.max_turn("s"), 1)

    def test_registry_cleanup_does_not_load_services_or_interrupt_active_generation(self):
        with patch("ember.generations.time.time", return_value=1000):
            self.start()
        registry = InstanceRegistry(self.tmp.name, ["default"])
        with patch("ember.generations.time.time", return_value=1000 + SNAPSHOT_TTL_SECONDS):
            self.assertEqual(registry.expire_recall_snapshots(), 1)
        self.assertEqual(registry._services, {})
        self.assertEqual(self.svc.generations.generation("r1")["state"], "running")

    def test_same_id_concurrent_start_and_query_report_pending_with_single_creator(self):
        entered, release = threading.Event(), threading.Event()
        original = self.svc.run_recall_pipeline
        calls = []
        def delayed(*args, **kwargs):
            calls.append(1)
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args, **kwargs)
        with patch.object(self.svc, "run_recall_pipeline", side_effect=delayed), ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.start)
            try:
                self.assertTrue(entered.wait(5))
                pending = self.start()
                self.assertEqual(pending["status"], "in_progress")
                self.assertNotIn("recall_refs", pending)
                self.assertEqual(self.svc.sidecar_recall_snapshot("r1")["status"], "in_progress")
            finally:
                release.set()
            first = future.result(timeout=5)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.start(), first)
        self.assertEqual(self.svc.sessions["s"].turn_counter, 1)

    def test_before_commit_failure_has_no_jsonl_effects_and_never_reruns_identity(self):
        with patch.object(self.svc.generations, "commit_snapshot", side_effect=OSError("before commit")):
            out = self.start()
        self.assertEqual(out["code"], "recall_outcome_unknown")
        self.assertEqual(self.svc.recall_ledger.event_recall_count, {})
        self.assertEqual(self.svc.turn_ledger.all_turns, set())
        self.assertNotIn(("s", 1), self.svc.turn_ledger.user_msg)
        self.assertEqual(self.svc.generations.pending_effects(), [])
        self.assertEqual(self.start()["code"], "recall_outcome_unknown")
        again = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.assertEqual(again.get_or_create_session("s").turn_counter, 1)
        next_turn = again.hook_turn_start("s", "no matching cues", user_message_id=105)
        self.assertEqual(next_turn["turn"], 2)

    def test_process_exit_before_result_commit_keeps_unknown_and_reserved_turn(self):
        with patch.object(self.svc, "run_recall_pipeline", side_effect=SystemExit("simulated process exit")):
            with self.assertRaises(SystemExit):
                self.start()
        again = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.assertEqual(again.sidecar_recall_snapshot("r1")["code"], "recall_outcome_unknown")
        self.assertEqual(again.generations.max_turn("s"), 1)
        self.assertEqual(again.recall_ledger.event_recall_count, {})

    def test_lost_ack_after_sqlite_commit_recovers_original_not_a_new_selection(self):
        commit = self.svc.generations.commit_snapshot
        def commit_then_fail(*args, **kwargs):
            commit(*args, **kwargs)
            raise OSError("lost commit acknowledgement")
        with patch.object(self.svc.generations, "commit_snapshot", side_effect=commit_then_fail):
            out = self.start()
        self.assertEqual(out["code"], "recall_recovery_pending")
        frozen = json.loads(self.svc.generations.snapshot("r1")["response_json"])
        again = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.assertEqual(again.hook_turn_start("s", "remember beacon", user_message_id=101, recall_id="r1"), frozen)
        self.assertEqual(again.recall_ledger.event_recall_count["A"], 1)
        self.assertEqual(len(again.turn_ledger.all_turns), 1)

    def test_effect_written_before_ack_is_deduplicated_after_restart_without_clock_refresh(self):
        ack = self.svc.generations.acknowledge_effect
        calls = []
        def fail_second(operation_id):
            calls.append(operation_id)
            if len(calls) == 2:
                raise OSError("after JSONL write before effect acknowledgement")
            ack(operation_id)
        with patch.object(self.svc.generations, "acknowledge_effect", side_effect=fail_second):
            out = self.start()
        self.assertEqual(out["code"], "recall_recovery_pending")
        self.assertEqual(self.svc.recall_ledger.event_recall_count["A"], 1)
        original_time = self.svc.recall_ledger.last_injected[("s", "A")]
        again = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.assertEqual(again.recall_ledger.event_recall_count["A"], 1)
        self.assertEqual(again.recall_ledger.last_injected[("s", "A")], original_time)
        self.assertEqual(again.generations.pending_effects(), [])
        self.assertEqual(len((Path(self.tmp.name) / "recall_ledger.jsonl").read_text().splitlines()), 1)
        self.assertIsNone(again._live_turn)

    def test_request_time_and_revisions_are_frozen_and_cannot_change_on_retry(self):
        values = {"occurred_at": "2026-10-20T08:00:00Z", "activity_revision": 4, "window_revision": 2}
        first = self.start(**values)
        self.assertEqual(self.start(**values), first)
        self.assertEqual(self.start(**dict(values, activity_revision=5))["code"], "id_conflict")
        request = self.svc.sidecar_recall_snapshot("r1")["snapshot"]["request_context"]
        self.assertEqual(request["activity_revision"], 4)
        self.assertEqual(request["window_revision"], 2)
        self.assertEqual(request["occurred_at"], values["occurred_at"])

    def test_invalid_versions_or_timestamps_never_register_a_generation(self):
        for values in ({"activity_revision": True}, {"window_revision": -1},
                       {"occurred_at": "not-a-date"}, {"occurred_at": "2026-10-20T08:00:00"}):
            self.assertEqual(self.start(**values)["code"], "invalid_arguments")
        self.assertIsNone(self.svc.generations.generation("r1"))

    def test_empty_recall_is_frozen_but_does_not_look_like_failed_recall(self):
        self.svc.pin_store.add("A")
        first = self.start()
        self.assertFalse(first["injected"])
        self.assertEqual(first["recall_refs"]["events"], [])
        self.assertEqual(self.svc.sidecar_recall_snapshot("r1")["status"], "ready")
        self.assertEqual(self.start(), first)

    def test_legacy_registration_without_snapshot_is_not_rerun_or_reported_empty(self):
        digest = fingerprint({"session_id": "s", "message": "remember beacon", "heart_turn_id": None,
                              "user_message_id": "101", "context_start_id": None})
        self.svc.generations.reserve("r1", "s", digest)
        self.assertEqual(self.start()["code"], "recall_snapshot_unavailable")
        self.assertEqual(self.svc.recall_ledger.event_recall_count, {})

    def test_queries_and_replay_do_not_reopen_superseded_context(self):
        first = self.start()
        self.start("r2")
        current = self.svc._live_turn
        self.assertEqual(self.start(), first)
        self.assertEqual(self.svc.sidecar_recall_snapshot("r1")["status"], "ready")
        self.assertIs(self.svc._live_turn, current)
        self.assertEqual(self.svc.generations.generation("r1")["state"], "superseded")

    def test_unknown_id_invalid_view_and_instance_isolation(self):
        self.start()
        self.assertEqual(self.svc.sidecar_recall_snapshot("missing")["code"], "unknown_recall")
        self.assertEqual(self.svc.sidecar_recall_snapshot("r1", "invalid")["code"], "invalid_arguments")
        with tempfile.TemporaryDirectory() as other:
            svc = EmberService(EmberStore(other), MockLLMClient())
            self.assertEqual(svc.sidecar_recall_snapshot("r1")["code"], "unknown_recall")

    def test_snapshot_transaction_failure_does_not_publish_partial_ready_state(self):
        self.svc.generations.reserve("r1", "s", "hash", 1, {})
        effects = [("turn_start", {"ember_session": "s", "ember_turn": 1}), ("invalid", {"bad": object()})]
        with self.assertRaises(TypeError):
            self.svc.generations.commit_snapshot("r1", 1, {"injected_text": "text"}, {}, effects)
        self.assertEqual(self.svc.generations.snapshot("r1")["state"], "in_progress")
        self.assertEqual(self.svc.generations.pending_effects(), [])
        self.assertEqual(self.svc.generations.generation("r1")["state"], "starting")


class EffectJournalTests(unittest.TestCase):
    def test_interrupted_jsonl_tail_and_duplicate_operation_are_recoverable(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "recall_ledger.jsonl").write_bytes(b'{"truncated":')
            ledger = RecallLedger(root)
            entry = ledger.make_entry("s", 1, "cue", ["A"])
            ledger.record_once("op", entry)
            ledger.record_once("op", entry)
            again = RecallLedger(root)
            self.assertEqual(again.event_recall_count["A"], 1)
            Path(root, "turn_ledger.jsonl").write_bytes(b'{"truncated":')
            turns = TurnLedger(root)
            row = {"ember_session": "s", "ember_turn": 1, "heart_turn_id": "t1", "user_message_id": 71}
            turns.record_start_once("start-op", row)
            turns.record_start_once("start-op", row)
            reloaded = TurnLedger(root)
            self.assertEqual(reloaded.user_msg[("s", 1)], 71)
            self.assertEqual(reloaded.reverse["t1"], [("s", 1)])


if __name__ == "__main__":
    unittest.main()
