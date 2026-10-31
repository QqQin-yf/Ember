"""Completed source generations enrich candidates, never frozen recall text."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ember.generations import SNAPSHOT_TTL_SECONDS
from ember.llm import MockLLMClient
from tests.legacy_event_fixture import LegacyEventService as EmberService
from ember.store import EmberStore
from tests.test_generations import TEXT
from tests.test_sidecar import write_args


class SourceCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.first = self.svc.hook_turn_start("source", TEXT, user_message_id=71, recall_id="writer")
        written = self.svc.sidecar_call_tool("create_memory_event", write_args(), recall_id="writer", tool_call_id="create")
        self.assertEqual(written["status"], "created", written)
        self.eid = written["event_id"]

    def recall(self, rid="reader", sid="reader-session", seq=101):
        result = self.svc.hook_turn_start(sid, "Ember", user_message_id=seq, recall_id=rid)
        self.assertEqual([e["event_id"] for e in result["recall_refs"]["events"]], [self.eid], result)
        return result

    def finish(self, status="done", seq=75, text="Committed original generation reply"):
        return self.svc.hook_turn_end("source", text if status == "done" else "",
            turn=1, assistant_message_id=seq if status == "done" else None,
            recall_id="writer", status=status)

    def view(self, rid="reader"):
        return self.svc.sidecar_recall_snapshot(rid)

    def test_capture_marks_exact_pending_source_and_done_enriches_new_recall(self):
        frozen = self.svc.event_sources.get(self.eid)
        self.assertEqual(frozen["pending_sources"], [{"turn": 1, "user_message_id": "71", "pending_source_recall_id": "writer"}])
        self.assertTrue(self.finish()["ok"])
        response = self.recall()
        ref = response["recall_refs"]["events"][0]
        self.assertEqual(ref["source_evidence"], "generation_completed")
        self.assertEqual(ref["turn_messages"][0]["assistant_message_id"], "75")
        self.assertEqual(ref["completion_bindings"], [{"turn": 1, "recall_id": "writer", "user_message_id": "71", "assistant_message_id": "75", "evidence": "turn_end", "status": "done"}])
        self.assertEqual(self.view()["sources_revision"], 0)
        self.assertEqual(self.svc.event_sources.get(self.eid), frozen)

    def test_existing_recall_changes_only_candidates_and_preserves_original_response(self):
        first = self.recall()
        old = self.view()
        before_events = Path(self.tmp.name, "events.jsonl").read_bytes()
        times = dict(self.svc.recall_ledger.last_extracted)
        current = self.svc._live_turn
        self.finish()
        new = self.view()
        self.assertEqual(new["sources_revision"], 1)
        self.assertEqual(new["snapshot"], old["snapshot"])
        self.assertIsNone(new["snapshot"]["recall_refs"]["events"][0]["turn_messages"][0]["assistant_message_id"])
        self.assertEqual(new["source_candidates"][0]["turn_messages"][0]["assistant_message_id"], "75")
        self.assertEqual(self.svc.sidecar_recall_snapshot("reader", "response")["response"], first)
        self.assertEqual(self.svc.hook_turn_start("reader-session", "Ember", user_message_id=101, recall_id="reader"), first)
        self.assertEqual(Path(self.tmp.name, "events.jsonl").read_bytes(), before_events)
        self.assertEqual(self.svc.recall_ledger.last_extracted, times)
        self.assertIs(self.svc._live_turn, current)
        self.assertEqual(self.view("writer")["source_candidates"], [])

    def test_done_retry_query_and_restart_never_increase_revision_twice(self):
        self.recall()
        ack = self.finish()
        self.assertEqual(self.finish(), ack)
        for _ in range(3):
            self.assertEqual(self.view()["sources_revision"], 1)
        self.svc = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.assertEqual(self.finish(), ack)
        self.assertEqual(self.view()["sources_revision"], 1)
        self.assertIsNone(self.svc._live_turn)

    def test_revision_transaction_rolls_back_and_recovers_from_done_receipt(self):
        self.recall()
        update = self.svc.source_completions._update_views
        def fail_after_update(*args):
            update(*args)
            raise OSError("simulated source transaction interruption")
        with patch.object(self.svc.source_completions, "_update_views", side_effect=fail_after_update):
            self.assertTrue(self.finish()["ok"])
            self.assertEqual(self.view()["code"], "recall_recovery_pending")
            with self.svc.generations._db() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM source_completions").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM recall_sources").fetchone()[0], 0)
        self.svc = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.assertEqual(self.view()["sources_revision"], 1)
        self.assertEqual(self.view()["source_candidates"][0]["source_evidence"], "generation_completed")

    def test_lost_terminal_ack_and_historical_done_recover_after_restart(self):
        self.recall()
        finish = self.svc.generations.finish
        def lost(*args):
            finish(*args)
            raise OSError("lost acknowledgement")
        with patch.object(self.svc.generations, "finish", side_effect=lost):
            with self.assertRaises(OSError):
                self.finish()
        self.svc = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.assertTrue(self.finish()["ok"])
        self.assertEqual(self.view()["sources_revision"], 1)

    def test_expired_response_still_exposes_completed_sources_without_extending_ttl(self):
        with patch("ember.generations.time.time", return_value=1000):
            self.recall()
        with patch("ember.generations.time.time", return_value=1000 + SNAPSHOT_TTL_SECONDS):
            self.finish()
            result = self.view()
            self.assertEqual(result["sources_revision"], 1)
            self.assertFalse(result["response_available"])
            self.assertEqual(self.svc.sidecar_recall_snapshot("reader", "response")["code"], "response_expired")
            self.assertEqual(self.svc.generations.snapshot("reader")["expires_at"], 1000 + SNAPSHOT_TTL_SECONDS)

    def test_cancelled_or_error_never_supply_assistant_evidence(self):
        self.recall()
        for status in ("cancelled", "error"):
            # Separate terminals: reset the test generation between subcases.
            with self.svc.generations._db() as db:
                db.execute("UPDATE generations SET state='running',end_hash=NULL,end_result=NULL WHERE recall_id='writer'")
            self.finish(status)
            self.assertEqual(self.view()["sources_revision"], 0)
            self.assertIsNone(self.view()["source_candidates"][0]["turn_messages"][0]["assistant_message_id"])

    def test_wrong_terminal_identity_and_content_conflict_cannot_replace_binding(self):
        self.recall()
        wrong = self.svc.hook_turn_end("wrong", "reply", turn=1, assistant_message_id=99, recall_id="writer")
        self.assertEqual(wrong["code"], "context_mismatch")
        self.assertEqual(self.view()["sources_revision"], 0)
        self.finish()
        self.assertEqual(self.finish(seq=99)["code"], "id_conflict")
        self.assertEqual(self.view()["source_candidates"][0]["turn_messages"][0]["assistant_message_id"], "75")

    def test_other_instance_has_no_bindings(self):
        self.recall()
        self.finish()
        with tempfile.TemporaryDirectory() as other:
            separate = EmberService(EmberStore(other), MockLLMClient())
            self.assertEqual(separate.sidecar_recall_snapshot("reader")["code"], "unknown_recall")

    def test_legacy_v1_can_repair_only_with_exact_durable_receipts(self):
        self._replace_source(lambda r: (r.update(schema_version=1), r.pop("pending_sources")))
        self.recall()
        self.finish()
        self.assertEqual(self.view()["sources_revision"], 1)

    def _replace_source(self, change):
        # Simulate an older or corrupted provenance file before a service restart.
        record = self.svc.event_sources.get(self.eid)
        change(record)
        Path(self.svc.event_sources.path).write_text(json.dumps(record) + "\n", encoding="utf-8")
        self.svc = EmberService(EmberStore(self.tmp.name), MockLLMClient())

    def test_unverified_corpus_and_wrong_user_anchor_are_not_repaired(self):
        self._replace_source(lambda r: r.update(identity_verified=False))
        self.recall()
        self.finish()
        self.assertEqual(self.view()["sources_revision"], 0)
        self.assertEqual(self.view()["source_candidates"][0]["source_evidence"], "unverified_corpus")

    def test_explicit_pending_mismatch_is_not_treated_as_legacy(self):
        self._replace_source(lambda r: r["pending_sources"][0].update(pending_source_recall_id="another"))
        self.recall()
        self.finish()
        self.assertEqual(self.view()["sources_revision"], 0)

    def test_unknown_or_wrong_tool_receipt_does_not_prove_event_creation(self):
        self.recall()
        with self.svc.generations._db() as db:
            db.execute("UPDATE calls SET state='unknown' WHERE recall_id='writer'")
        self.finish()
        self.assertEqual(self.view()["sources_revision"], 0)

    def test_tool_receipt_for_another_event_cannot_supply_evidence(self):
        self.recall()
        with self.svc.generations._db() as db:
            db.execute("UPDATE calls SET result_json=? WHERE recall_id='writer'", (
                json.dumps({"status": "created", "event_id": "another-event"}),))
        self.finish()
        self.assertEqual(self.view()["sources_revision"], 0)

    def test_legacy_wrong_anchor_and_cross_session_cannot_be_repaired(self):
        self._replace_source(lambda r: (r.update(schema_version=1, source_session_id="another-session"),
                                       r["turn_messages"][0].update(user_message_id="999")))
        self.recall()
        self.finish()
        self.assertEqual(self.view()["sources_revision"], 0)

    def test_legacy_wrong_anchor_with_matching_session_is_still_rejected(self):
        self._replace_source(lambda r: (r.update(schema_version=1),
                                       r["turn_messages"][0].update(user_message_id="999")))
        self.recall()
        self.finish()
        self.assertEqual(self.view()["sources_revision"], 0)

    def test_known_original_assistant_is_not_replaced_by_writer_reply(self):
        self._replace_source(lambda r: r["turn_messages"][0].update(assistant_message_id="55"))
        self.recall()
        self.finish()
        self.assertEqual(self.view()["sources_revision"], 0)
        self.assertEqual(self.view()["source_candidates"][0]["turn_messages"][0]["assistant_message_id"], "55")

    def test_snapshot_with_different_known_messages_is_not_repaired(self):
        self.recall()
        with self.svc.generations._db() as db:
            row = db.execute("SELECT metadata_json FROM recall_snapshots WHERE recall_id='reader'").fetchone()
            metadata = json.loads(row[0])
            metadata["recall_refs"]["events"][0]["turn_messages"][0]["assistant_message_id"] = "55"
            db.execute("UPDATE recall_snapshots SET metadata_json=? WHERE recall_id='reader'", (json.dumps(metadata),))
        self.finish()
        self.assertEqual(self.view()["sources_revision"], 0)

    def test_no_source_binding_is_published_before_real_done(self):
        self.recall()
        self.assertEqual(self.view()["sources_revision"], 0)
        with self.svc.generations._db() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM source_completions").fetchone()[0], 0)

    def test_multiturn_missing_history_stays_missing(self):
        self._replace_source(lambda r: (r.update(source_turn_spans=[[1, 2]]), r["turn_messages"].append(
            {"turn": 2, "user_message_id": None, "assistant_message_id": None})))
        self.recall()
        self.finish()
        ref = self.view()["source_candidates"][0]
        self.assertEqual(ref["reason"], "source_seq_missing")
        self.assertEqual(ref["completion_bindings"][0]["turn"], 1)
        self.assertIsNone(ref["turn_messages"][1]["assistant_message_id"])

    def test_changed_snapshot_source_or_content_version_is_never_overwritten(self):
        self.recall()
        with self.svc.generations._db() as db:
            row = db.execute("SELECT metadata_json FROM recall_snapshots WHERE recall_id='reader'").fetchone()
            metadata = json.loads(row[0])
            metadata["recall_refs"]["events"][0]["version_id"] = "different-version"
            db.execute("UPDATE recall_snapshots SET metadata_json=? WHERE recall_id='reader'", (json.dumps(metadata),))
        self.finish()
        self.assertEqual(self.view()["sources_revision"], 0)

    def test_each_old_recall_has_its_own_revision_and_new_recall_starts_at_zero(self):
        self.recall()
        self.recall("reader2", "reader2-session", 201)
        self.finish()
        self.assertEqual(self.view("reader")["sources_revision"], 1)
        self.assertEqual(self.view("reader2")["sources_revision"], 1)
        self.recall("reader3", "reader3-session", 301)
        self.assertEqual(self.view("reader3")["sources_revision"], 0)
        self.assertEqual(self.view("reader3")["source_candidates"][0]["source_evidence"], "generation_completed")


if __name__ == "__main__":
    unittest.main()
