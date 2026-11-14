"""Generation-scoped writes, durable receipts, race/restart boundaries and HTTP wiring."""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from ember.generations import fingerprint
from ember.llm import MockLLMClient
from ember.service import EmberService
from ember.store import EmberStore
from tests.test_sidecar import write_args

TEXT = "今天我们决定把 Ember 的传播模型改成 Route Search。"


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.no_fetch = patch.object(self.svc, "_fetched_corpus", return_value=[])
        self.no_fetch.start()
        self.addCleanup(self.no_fetch.stop)

    def start(self, rid="r-1", sid="s1", seq=71, text=TEXT):
        return self.svc.hook_turn_start(sid, text, user_message_id=seq, recall_id=rid)

    def call(self, rid="r-1", cid="tool-1", args=None, name="write_event"):
        return self.svc.sidecar_call_tool(name, write_args() if args is None else args,
                                          recall_id=rid, tool_call_id=cid)

    def test_duplicate_start_does_not_recall_allocate_or_reopen_old_context(self):
        first = self.start()
        second = self.start()
        self.assertEqual(second, first)
        self.assertEqual(self.svc.sessions["s1"].turn_counter, 1)
        self.start("r-2", "s2", 80)
        current = self.svc._live_turn
        self.start()
        self.assertIs(self.svc._live_turn, current)
        self.assertEqual(self.svc.generations.generation("r-1")["state"], "superseded")
        self.assertEqual(first["recall_refs"]["recall_id"], "r-1")

    def test_cc_and_app_write_independently_with_main_evidence(self):
        past = "Past computer conversation"
        phone = "Past phone conversation"
        rows = [
            {"message_id": 11, "role": "user", "content": past},
            {"message_id": 12, "role": "assistant", "content": "Computer reply"},
            {"message_id": 21, "role": "user", "content": phone},
            {"message_id": 22, "role": "assistant", "content": "Phone reply"},
        ]
        with patch.object(self.svc, "run_recall_pipeline", side_effect=AssertionError("historical recall")):
            for i, text in ((11, past), (21, phone)):
                rid = f"history-{i}"
                started = self.svc.hook_turn_start("main", text, user_message_id=i, recall_id=rid, historical=True)
                replay = self.svc.hook_turn_start("main", text, user_message_id=i, recall_id=rid, historical=True)
                self.assertEqual(started, replay)
                self.assertEqual(self.svc.generations.generation(rid)["state"], "registered")
                end = self.svc.hook_turn_end("main", rows[1 if i == 11 else 3]["content"],
                                             started["turn"], assistant_message_id=i + 1, recall_id=rid)
                self.assertTrue(end["ok"], end)

        self.start("app-one", "main", 31)
        self.assertTrue(self.svc.cc_turn_start("window-one", "cc-one", "main-leaf")["ok"])
        self.assertEqual(self.svc.generations.generation("app-one")["state"], "running")
        app_event = self.call("app-one")
        self.assertEqual(app_event["status"], "created", app_event)

        self.start("app-two", "main", 33)
        self.assertEqual(self.svc.generations.generation("cc-one")["state"], "running")
        with patch.object(self.svc, "run_recall_pipeline", side_effect=AssertionError("historical recall")):
            late = self.svc.hook_turn_start("main", "Late CC original", user_message_id=40,
                                            recall_id="late-history", historical=True)
            self.assertIn("turn", late)
        self.assertEqual(self.svc.generations.generation("cc-one")["state"], "running")
        self.assertEqual(self.svc.generations.generation("app-two")["state"], "running")
        args = write_args(quote_spans=[{"start_quote": past, "end_quote": phone}])
        args["event"]["focus"] = past
        def pages():
            yield [rows[3]]
            yield [rows[2], rows[1]]
            yield [rows[0]]
            self.fail("Source search continued after both endpoints and their pairs were found")

        with patch("ember.source_client.iter_source_branch", side_effect=lambda *a, **kw: pages()) as fetch:
            result = self.svc.sidecar_call_tool("write_event", args, domain="cc")
            self.assertEqual(result["status"], "created", result)
            evidence = self.svc.event_sources.get(result["event_id"])
            self.assertEqual(evidence["source_session_id"], "main")
            self.assertEqual([(m["user_message_id"], m["assistant_message_id"])
                              for m in evidence["turn_messages"]], [("11", "12"), ("21", "22")])
            revised = self.svc.sidecar_call_tool("revise_memory_event", {
                "event_id": result["event_id"], "summary": "Revised from the CC branch",
            }, domain="cc")
            self.assertEqual(revised["status"], "revised", revised)
            args["event"]["kind"] = "promise"
            result = self.svc.sidecar_call_tool("write_promise", args, domain="cc")
            self.assertEqual(result["status"], "created", result)
            self.assertEqual(self.svc.store.get_event(result["event_id"]).kind, "promise")
            args["event"]["kind"] = "project"
            fetch.side_effect = None
            fetch.return_value = [list(reversed(rows))]
            args["quote_spans"][0]["end_quote"] = "The unsaved current prompt"
            result = self.svc.sidecar_call_tool("write_event", args, domain="cc")
            self.assertEqual(result["code"], "cc_quote_not_in_history")
            fetch.assert_called_with("main", "main-leaf", instance="default")
            args["quote_spans"][0]["end_quote"] = phone
            fetch.return_value = [[*rows, {"message_id": 29, "role": "user", "content": past}]]
            result = self.svc.sidecar_call_tool("write_event", args, domain="cc")
            self.assertEqual(result["code"], "quote_ambiguous")

        self.assertTrue(self.svc.cc_turn_start("window-two", "cc-two", "new-leaf")["ok"])
        self.svc.cc_turn_end("window-one", "cc-one")
        self.svc.cc_turn_end("window-two", "cc-one")
        self.assertEqual(self.svc.generations.generation("cc-two")["state"], "running")
        self.svc.cc_turn_end("window-two", "cc-two")
        self.assertEqual(self.svc.sidecar_call_tool("write_event", args, domain="cc")["code"], "cc_turn_missing")
        self.assertEqual(self.svc.generations.generation("app-two")["state"], "running")

    def test_hook_timing_separates_lock_wait_and_execution(self):
        with patch("ember.service.time.monotonic", side_effect=[0, 1.25, 1.5]):
            with self.assertLogs("ember.service", level="WARNING") as logs:
                self.svc.cc_turn_start("window", "live-timing", "anchor")
        self.assertIn("operation=cc/start lock_wait_ms=1250 execution_ms=250", logs.output[0])

    def test_cc_history_recovers_its_ledger_receipt_without_projecting_all_sources(self):
        kwargs = dict(user_message_id=41, recall_id="history-only", historical=True)
        with patch.object(self.svc.source_completions, "reconcile", side_effect=AssertionError("global projection")):
            with patch.object(self.svc.turn_ledger, "record_start_once", side_effect=OSError("write unavailable")):
                pending = self.svc.hook_turn_start("main", "Original message", **kwargs)
                self.assertEqual(pending["code"], "recall_recovery_pending")
            receipt = self.svc.hook_turn_start("main", "Original message", **kwargs)
            self.assertEqual(self.svc.turn_ledger.user_msg["main", receipt["turn"]], 41)
            self.assertEqual(self.svc.hook_turn_start("main", "Original message", **kwargs), receipt)
            self.assertFalse(self.svc.generations.pending_effects("history-only"))

    def test_cc_quote_reports_unregistered_and_incomplete_turns(self):
        user = {"message_id": 41, "role": "user", "content": "Original user message"}
        reply = {"message_id": 42, "role": "assistant", "content": "Original assistant reply"}
        self.svc.cc_turn_start("window", "cc-window", "main-leaf")
        args = write_args(quote_spans=[{"start_quote": user["content"], "end_quote": user["content"]}])
        args["event"]["focus"] = user["content"]
        with patch("ember.source_client.iter_source_branch") as fetch:
            fetch.return_value = [[reply, dict(user, cc_turn_complete=True)]]
            missing = self.svc.sidecar_call_tool("write_event", args, domain="cc")
            self.assertEqual(missing["code"], "cc_quote_unregistered")
            self.assertEqual(missing["field"], "quote_spans[0].start_quote")
            self.assertIn("补登记", missing["hint"])

            fetch.return_value = [[dict(user, cc_turn_complete=False)]]
            partial = self.svc.sidecar_call_tool("write_event", args, domain="cc")
            self.assertEqual(partial["code"], "cc_quote_incomplete_turn")
            self.assertIn("换用", partial["hint"])

            started = self.svc.hook_turn_start("main", user["content"], user_message_id=41,
                                               recall_id="historical", historical=True)
            fetch.return_value = [[reply, dict(user, cc_turn_complete=True)]]
            self.assertEqual(self.svc.sidecar_call_tool("write_event", args, domain="cc")["code"],
                             "cc_quote_unregistered")
            self.svc.hook_turn_end("main", reply["content"], started["turn"],
                                   assistant_message_id=42, recall_id="historical")
            self.assertEqual(self.svc.sidecar_call_tool("write_event", args, domain="cc")["status"], "created")

    def test_cc_source_page_budget_and_cursor(self):
        from ember import source_client

        def page(_sid, _anchor, *, instance, before, limit):
            self.assertEqual(instance, "default")
            self.assertEqual(limit, 200)
            start = int(before) if before else 6001
            rows = [{"message_id": str(seq)} for seq in range(start - 1, start - limit - 1, -1)]
            return rows, rows[-1]["message_id"]

        with patch.object(source_client, "fetch_source_branch", side_effect=page) as fetch:
            with self.assertRaises(source_client.SourceUnavailable) as error:
                list(source_client.iter_source_branch("main", "leaf", instance="default"))
            self.assertEqual(error.exception.code, "cc_history_limit")
            self.assertEqual(fetch.call_count, 25)
            self.assertEqual(fetch.call_args.kwargs["before"], "1201")

    def test_changed_start_identity_conflicts_without_mutation(self):
        self.start()
        out = self.start(text="changed user input")
        self.assertEqual(out["code"], "id_conflict")
        self.assertEqual(self.svc.sessions["s1"].turn_counter, 1)

    def test_missing_real_user_identity_does_not_register_generation(self):
        out = self.svc.hook_turn_start("s", TEXT, recall_id="r", user_message_id=None)
        self.assertEqual(out["code"], "invalid_identity")
        self.assertIsNone(self.svc.generations.generation("r"))

    def test_bound_write_ignores_poisoned_shared_live_slot(self):
        self.start()
        self.svc._live_turn = {"session": self.svc.get_or_create_session("wrong"), "turn": 99}
        out = self.call()
        self.assertEqual(out["status"], "created", out)
        ev = self.svc.store.get_event(out["event_id"])
        self.assertEqual(ev.source_ref, "conversation/session-s1")
        self.assertEqual(ev.turn_refs, ["turn-1"])
        self.assertEqual(self.svc.event_sources.get(ev.event_id)["writer_recall_id"], "r-1")

    def test_no_header_or_partial_or_unknown_context_never_borrows_bound_slot(self):
        self.start()
        self.assertEqual(self.svc.sidecar_call_tool("write_event", write_args())["code"], "identity_required")
        self.assertEqual(self.svc.sidecar_create_memory_event(write_args())["code"], "identity_required")
        self.assertEqual(self.svc.sidecar_call_tool("write_event", write_args(), recall_id="r-1")["code"], "invalid_identity")
        self.assertEqual(self.call(rid="unknown")["code"], "unknown_recall")
        self.assertEqual(len(self.svc.store.all_events()), 0)

    def test_run_or_mixed_identity_does_not_fall_back_to_dialogue(self):
        self.start()
        self.assertEqual(self.svc.sidecar_call_tool("write_event", write_args(), run_id="run1")["code"], "invalid_identity")
        self.assertEqual(self.svc.sidecar_call_tool("write_event", write_args(), run_id="run1", recall_id="r-1", tool_call_id="t")["code"], "context_mismatch")
        self.assertEqual(len(self.svc.store.all_events()), 0)

    def test_completed_write_retry_survives_supersession_and_restart(self):
        self.start()
        first = self.call()
        self.start("r-2", "s2", 80)
        self.assertEqual(self.call(), first)
        self.assertEqual(self.call(cid="late-new")["code"], "stale_generation")
        again = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        out = again.sidecar_call_tool("write_event", write_args(), recall_id="r-1", tool_call_id="tool-1")
        self.assertEqual(out, first)
        self.assertEqual(len(again.store.all_events()), 1)

    def test_source_bound_write_retry_survives_restart(self):
        binding = {"source_session": "s1", "source_user": "71",
                   "source_assistant": "72", "tool_call_id": "source-tool"}
        rows = [
            {"message_id": 71, "role": "user", "id": "u1", "content": TEXT},
            {"message_id": 72, "role": "assistant", "parent_id": "u1", "content": ""},
        ]
        with patch("ember.source_client.fetch_source_path", return_value=rows):
            first = self.svc.sidecar_call_tool("write_event", write_args(), **binding)
        self.assertEqual(first["status"], "created", first)
        again = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        with patch("ember.source_client.fetch_source_path", side_effect=AssertionError("receipt must replay")):
            replayed = again.sidecar_call_tool("write_event", write_args(), **binding)
            conflict = again.sidecar_call_tool("write_event", write_args(cues=("changed",)), **binding)
        self.assertEqual(replayed, first)
        self.assertEqual(conflict["code"], "id_conflict")
        self.assertEqual(len(again.store.all_events()), 1)

    def test_tool_conflicts_are_scoped_to_generation_and_do_not_block_new_calls(self):
        self.start()
        self.call()
        args = write_args()
        args["event"]["core"] = "different memory"
        self.assertEqual(self.call(args=args)["code"], "id_conflict")
        self.assertEqual(self.call(name="pin_memory", args={"event_id": "A"})["code"], "id_conflict")
        self.start("r-2", "s2", 80)
        args=write_args()
        separate = self.call(rid="r-2",args=args)
        self.assertEqual(separate["status"], "created")
        self.assertEqual(self.svc.store.get_event(separate["event_id"]).source_ref, "conversation/session-s2")
        self.assertEqual(len(self.svc.store.all_events()), 2)

    def test_concurrent_identical_calls_only_write_once(self):
        self.start()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.call(), range(8)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(results[0]["status"], "created")
        self.assertEqual(len(self.svc.store.all_events()), 1)

    def test_failure_receipt_is_stable_and_cannot_be_reused_with_corrected_arguments(self):
        self.start()
        bad = self.call(args={})
        self.assertEqual(bad["status"], "failed")
        self.assertEqual(self.call(args={}), bad)
        self.assertEqual(self.call()["code"], "id_conflict")
        self.assertEqual(self.call(cid="new-corrected")["status"], "created")

    def test_search_side_effects_are_not_repeated(self):
        self.start()
        with patch.object(self.svc, "sidecar_search_memory", return_value={"status": "ok", "items": []}) as search:
            one = self.call(args={"query": "x"}, name="search_memory")
            two = self.call(args={"query": "x"}, name="search_memory")
            self.assertEqual(one, two)
            self.assertEqual(search.call_count, 1)

    def test_pin_receipt_preserves_first_result_instead_of_reexecuting(self):
        self.start()
        made = self.call()
        args = {"event_id": made["event_id"]}
        one = self.call(cid="pin", args=args, name="pin_memory")
        self.assertFalse(one["already"])
        self.assertEqual(self.call(cid="pin", args=args, name="pin_memory"), one)

    def test_unknown_after_effect_is_never_blindly_replayed(self):
        self.start()
        original = self.svc._dispatch_memory_tool
        def mutate_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("receipt boundary failure")
        with patch.object(self.svc, "_dispatch_memory_tool", side_effect=mutate_then_fail):
            out = self.call()
        self.assertEqual(out["code"], "tool_outcome_unknown")
        self.assertEqual(len(self.svc.store.all_events()), 1)
        self.assertEqual(self.call()["code"], "tool_outcome_unknown")
        self.assertEqual(len(self.svc.store.all_events()), 1)
        self.assertEqual(self.svc.sidecar_tool_status("r-1", "tool-1")["status"], "unknown")

    def test_receipt_storage_failure_leaves_an_accepted_nonreplayable_call(self):
        import sqlite3
        self.start()
        with patch.object(self.svc.generations, "complete_call", side_effect=sqlite3.OperationalError("disk unavailable")), patch.object(self.svc.generations, "unknown_call", side_effect=sqlite3.OperationalError("disk unavailable")):
            out = self.call()
        self.assertEqual(out["code"], "tool_outcome_unknown")
        self.assertEqual(len(self.svc.store.all_events()), 1)
        self.assertEqual(self.call()["code"], "tool_outcome_unknown")
        self.assertEqual(len(self.svc.store.all_events()), 1)

    def test_restart_preserves_uncertain_call_and_interrupts_active_writer(self):
        self.start()
        self.svc.generations.begin_call("r-1", "unfinished", fingerprint({"recall_id": "r-1", "name": "write_event", "arguments": write_args()}), "write_event")
        again = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.assertEqual(again.sidecar_generation_status("r-1")["status"], "interrupted")
        self.assertEqual(again.sidecar_call_tool("write_event", write_args(), recall_id="r-1", tool_call_id="unfinished")["code"], "tool_outcome_unknown")
        self.assertEqual(again.sidecar_call_tool("write_event", write_args(), recall_id="r-1", tool_call_id="new")["code"], "stale_generation")

    def test_read_results_never_persist_transcript(self):
        self.start(text=TEXT + " user-message-do-not-store")
        secret = "read-transcript-do-not-store"
        with patch.object(self.svc, "sidecar_read_memory_source", return_value={"status": "ok", "transcript": secret}):
            one = self.call(args={"event_id": "A"}, name="read_memory_source")
            self.assertEqual(self.call(args={"event_id": "A"}, name="read_memory_source"), one)
        self.assertNotIn(secret.encode(), Path(self.svc.generations.path).read_bytes())
        self.assertNotIn(b"user-message-do-not-store", Path(self.svc.generations.path).read_bytes())
        again = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.assertEqual(again.sidecar_call_tool("read_memory_source", {"event_id": "A"}, recall_id="r-1", tool_call_id="tool-1")["code"], "result_not_retained")

    def test_end_requires_identity_and_is_idempotent_without_adding_reply_twice(self):
        self.start()
        self.assertEqual(self.svc.hook_turn_end("s1", "reply", turn=1, assistant_message_id=75)["code"], "identity_required")
        self.assertEqual(self.svc.hook_turn_end("wrong", "reply", turn=1, assistant_message_id=75, recall_id="r-1")["code"], "context_mismatch")
        one = self.svc.hook_turn_end("s1", "reply", turn=1, assistant_message_id=75, recall_id="r-1")
        self.assertTrue(one["ok"])
        self.assertEqual(self.svc.hook_turn_end("s1", "reply", turn=1, assistant_message_id=75, recall_id="r-1"), one)
        self.assertEqual(len([m for m in self.svc.sessions["s1"].messages if m["role"] == "assistant"]), 1)
        self.assertEqual(self.call()["code"], "stale_generation")
        self.assertEqual(self.svc.hook_turn_end("s1", "changed", turn=1, assistant_message_id=75, recall_id="r-1")["code"], "id_conflict")

    def test_late_old_completion_does_not_close_or_pollute_new_generation(self):
        self.start()
        self.start("r-2", "s2", 80)
        current = self.svc._live_turn
        out = self.svc.hook_turn_end("s1", "late historical reply", turn=1, assistant_message_id=75, recall_id="r-1")
        self.assertTrue(out["ok"])
        self.assertIs(self.svc._live_turn, current)
        self.assertEqual(self.svc.sidecar_generation_status("r-2")["status"], "running")
        self.assertFalse(any(m.get("content") == "late historical reply" for m in self.svc.sessions["s1"].messages))
        self.assertEqual(self.svc.turn_ledger.reply_msg[("s1", 1)], 75)

    def test_committed_completion_can_be_backfilled_after_restart(self):
        self.start()
        again = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        out = again.hook_turn_end("s1", "already committed reply", turn=1, assistant_message_id=75, recall_id="r-1")
        self.assertTrue(out["ok"])
        self.assertEqual(again.sidecar_generation_status("r-1")["status"], "done")
        self.assertIsNone(again._live_turn)
        self.assertEqual(again.hook_turn_end("s1", "already committed reply", turn=1, assistant_message_id=75, recall_id="r-1"), out)

    def test_cancelled_finish_never_adds_an_assistant_message(self):
        self.start()
        out = self.svc.hook_turn_end("s1", "partial not committed", recall_id="r-1", status="cancelled")
        self.assertTrue(out["ok"])
        self.assertEqual(self.svc.sidecar_generation_status("r-1")["status"], "cancelled")
        self.assertEqual(len(self.svc.sessions["s1"].messages), 1)

    def test_writer_generation_is_not_mistaken_for_historical_source_generation(self):
        self.start("r-old", "s1", 71)
        self.svc.hook_turn_end("s1", "已经确认按 Route Search 推进。", turn=1, assistant_message_id=75, recall_id="r-old")
        self.start("r-new", "s1", 80, "记下刚才决定。")
        self.no_fetch.stop()
        def fetch(sid, ids, **kw):
            return [{"message_id": int(m["_source_message_id"]), "role": m["role"], "content": m["content"]}
                    for m in self.svc.sessions[sid].messages if m.get("_source_message_id") in ids]
        transport = patch("ember.source_client.fetch_source_path", side_effect=fetch)
        transport.start()
        self.addCleanup(transport.stop)
        args = write_args(quote_spans=[{"start_quote": "今天我们决定把", "end_quote": "已经确认按 Route Search 推进。"}])
        made = self.call(rid="r-new", args=args)
        self.assertEqual(made["status"], "created", made)
        src = self.svc.event_sources.get(made["event_id"])
        self.assertEqual(src["writer_recall_id"], "r-new")
        self.assertEqual(src["writer_tool_call_id"], "tool-1")
        self.assertEqual(src["turn_messages"][0]["source_recall_id"], "r-old")
        self.assertEqual(src["turn_messages"][0]["assistant_message_id"], "75")

    def test_same_ids_in_other_instance_do_not_share_receipts(self):
        self.start()
        self.call()
        with tempfile.TemporaryDirectory() as other:
            other_svc = EmberService(EmberStore(other), MockLLMClient())
            self.assertEqual(other_svc.sidecar_call_tool("write_event", write_args(), recall_id="r-1", tool_call_id="tool-1")["code"], "unknown_recall")

    def test_query_metadata_excludes_request_hash_and_chat_text(self):
        self.start()
        self.call()
        row = self.svc.sidecar_generation_status("r-1")
        self.assertNotIn("request_hash", row)
        self.assertNotIn("message", row)
        self.assertEqual(row["calls"][0]["state"], "completed")
        self.assertEqual(self.svc.sidecar_tool_status("wrong", "tool-1")["code"], "unknown_tool_call")


@unittest.skipUnless(importlib.util.find_spec("mcp") and importlib.util.find_spec("httpx"), "MCP HTTP dependencies unavailable")
class GenerationHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch("ember.env.load_env"), patch.dict(os.environ, {"EMBER_DATA_ROOT": self.tmp.name, "EMBER_INSTANCES": "default,second", "EMBER_TOKEN": ""}):
            self.http = importlib.import_module("sidecar")
        from ember.instances import InstanceRegistry
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        import httpx
        self.registry = InstanceRegistry(self.tmp.name, ["default", "second"])
        self.reg_patch = patch.object(self.http, "REGISTRY", self.registry)
        self.reg_patch.start()
        self.addCleanup(self.reg_patch.stop)
        self.token_patch = patch.object(self.http, "TOKEN", "")
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.manager = StreamableHTTPSessionManager(app=self.http.mcp_server, json_response=True, stateless=True)
        self.manager_patch = patch.object(self.http, "session_manager", self.manager)
        self.manager_patch.start()
        self.addCleanup(self.manager_patch.stop)
        import asyncio
        ready, stop = asyncio.Event(), asyncio.Event()
        async def serve():
            async with self.manager.run():
                ready.set()
                await stop.wait()
        task = asyncio.create_task(serve())
        await ready.wait()
        async def shutdown():
            stop.set()
            await task
        self.addAsyncCleanup(shutdown)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.http.app), base_url="http://testserver",
            headers={"Accept": "application/json, text/event-stream"})
        self.addAsyncCleanup(self.client.aclose)

    async def start(self, instance="default", sid="s1", rid="r-1", seq="71"):
        return await self.client.post("/ember/hooks/turn_start", headers={"X-Heart-Instance": instance},
            json={"session_id": sid, "message": TEXT, "user_message_id": seq, "recall_id": rid})

    async def tool(self, headers=None, args=None, name="write_event"):
        response = await self.client.post("/mcp/", headers=headers or {}, json={"jsonrpc": "2.0", "id": 7,
            "method": "tools/call", "params": {"name": name, "arguments": args or write_args()}})
        if response.status_code != 200:
            return response, response.json()
        data = response.json()
        self.assertNotIn("error", data, data)
        return response, json.loads(data["result"]["content"][0]["text"])

    async def test_cc_mcp_uses_cc_slot_without_app_identity_headers(self):
        self.assertEqual((await self.start(sid="main")).status_code, 200)
        body = {"cc_session": "window", "recall_id": "cc-turn", "anchor": None}
        opened = await self.client.post("/ember/hooks/cc/start", json=body)
        self.assertEqual(opened.json(), {"ok": True})
        history = await self.client.post("/ember/hooks/cc/history", json={
            "session_id": "main", "user_message_id": 81, "message": "Historical original",
            "recall_id": "history-over-http",
        })
        self.assertEqual(history.status_code, 200, history.text)
        self.assertFalse(history.json()["injected"])
        tool = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "write_event", "arguments": write_args()}}
        response = await self.client.post("/mcp/cc/", json=tool)
        self.assertEqual(response.status_code, 200, response.text)
        result = json.loads(response.json()["result"]["content"][0]["text"])
        self.assertEqual(result["code"], "cc_quote_not_in_history")
        self.assertEqual((await self.client.post("/ember/hooks/cc/end", json=body)).json(), {"ok": True})
        _, app = await self.tool({"X-Ember-Recall-Id": "r-1", "X-Ember-Tool-Call-Id": "app-tool"})
        self.assertEqual(app["status"], "created", app)

    async def test_unbound_search_after_recall_failure_with_another_live_turn(self):
        svc = self.registry.service("default")
        with patch.object(svc, "run_recall_pipeline", side_effect=RuntimeError("recall failed")):
            failed = await self.start(sid="failed", rid="failed")
        self.assertEqual(failed.json()["code"], "recall_outcome_unknown")
        self.assertEqual((await self.start()).status_code, 200)
        _, result = await self.tool(args={"query": "Ember"}, name="search_memory")
        self.assertEqual(result["status"], "ok", result)

    async def test_fallback_writes_bind_exact_messages_and_replay_in_parallel(self):
        import asyncio

        svc = self.registry.service("default")
        with patch.object(svc, "run_recall_pipeline", side_effect=RuntimeError("recall failed")):
            for rid, seq in (("failed-1", "71"), ("failed-2", "81")):
                failed = await self.start(sid="s1", rid=rid, seq=seq)
                self.assertEqual(failed.json()["code"], "recall_outcome_unknown")
        rows = {
            71: {"role": "user", "id": "u1", "content": TEXT},
            72: {"role": "assistant", "id": "a1", "parent_id": "u1", "content": ""},
            81: {"role": "user", "id": "u2", "content": TEXT},
            82: {"role": "assistant", "id": "a2", "parent_id": "u2", "content": ""},
        }

        def source(sid, ids, *, instance, write_turn):
            self.assertEqual((sid, instance, write_turn), ("s1", "default", True))
            return [dict(rows[int(seq)], message_id=int(seq)) for seq in ids]

        def headers(user, reply):
            return {"X-Ember-Source-Session": "s1", "X-Ember-Source-User": str(user),
                    "X-Ember-Source-Assistant": str(reply), "X-Ember-Tool-Call-Id": "same-tool"}

        promise = write_args()
        promise["event"]["kind"] = "promise"
        with patch("ember.source_client.fetch_source_path", side_effect=source):
            _, crossed = await self.tool(headers(71, 82))
            self.assertEqual(crossed["code"], "source_path_unknown")
            results = await asyncio.gather(self.tool(headers(71, 72)),
                                           self.tool(headers(81, 82), promise, name="write_promise"),
                                           self.tool(headers(71, 72)))
        self.assertEqual(results[0][1], results[2][1])
        for (_, result), user, reply in zip(results[:2], (71, 81), (72, 82), strict=True):
            self.assertEqual(result["status"], "created", result)
            frozen = svc.event_sources.get(result["event_id"])
            self.assertEqual(frozen["source_session_id"], "s1")
            self.assertNotIn("writer_recall_id", frozen)
            [messages] = frozen["turn_messages"]
            self.assertEqual((messages["user_message_id"], messages["assistant_message_id"]),
                             (str(user), str(reply)))
        self.assertEqual(len(svc.store.all_events()), 2)
        self.assertFalse(svc.recall_ledger.by_turn)

    async def test_real_mcp_headers_select_bound_context_and_replay_receipt(self):
        self.assertEqual((await self.start()).status_code, 200)
        headers = {"X-Ember-Recall-Id": "r-1", "X-Ember-Tool-Call-Id": "tool-1"}
        _, result = await self.tool(headers)
        self.assertEqual(result["status"], "created", result)
        _, again = await self.tool(headers)
        self.assertEqual(again, result)
        self.assertIsNone(self.http.CURRENT_CALL_CONTEXT.get())
        receipt = await self.client.get("/ember/generations/r-1/tools/tool-1")
        self.assertEqual(receipt.status_code, 200)
        self.assertEqual(receipt.json()["result"], result)

    async def test_mcp_business_validation_failure_replays_as_tool_error(self):
        await self.start()
        headers = {"X-Ember-Recall-Id": "r-1", "X-Ember-Tool-Call-Id": "invalid-tool"}
        args = write_args()
        args["recall_routes"] = []
        body = {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                "params": {"name": "write_event", "arguments": args}}
        first = (await self.client.post("/mcp/", headers=headers, json=body)).json()["result"]
        again = (await self.client.post("/mcp/", headers=headers, json=body)).json()["result"]
        self.assertTrue(first["isError"])
        self.assertEqual(first, again)
        result = json.loads(first["content"][0]["text"])
        self.assertEqual(result["field"], "recall_routes")
        self.assertTrue(result["hint"])
        self.assertFalse(self.registry.service("default").store.all_events())

    async def test_concurrent_instance_headers_do_not_leak_context(self):
        import asyncio
        await self.start(sid="session-A")
        await self.start(instance="second", sid="session-B")
        requests = [self.tool({"X-Heart-Instance": inst, "X-Ember-Recall-Id": "r-1", "X-Ember-Tool-Call-Id": "same-id"}) for inst in ("default", "second")]
        results = await asyncio.gather(*requests)
        for inst, sid, (_, result) in zip(("default", "second"), ("session-A", "session-B"), results):
            self.assertEqual(result["status"], "created", result)
            self.assertEqual(self.registry.service(inst).store.get_event(result["event_id"]).source_ref, "conversation/session-" + sid)

    async def test_missing_unknown_and_active_run_headers_cannot_fall_back(self):
        await self.start()
        _, no_header = await self.tool()
        self.assertEqual(no_header["code"], "identity_required")
        _, unknown = await self.tool({"X-Ember-Recall-Id": "missing", "X-Ember-Tool-Call-Id": "t"})
        self.assertEqual(unknown["code"], "unknown_recall")
        _, active = await self.tool({"X-Ember-Run-Id": "run1", "X-Ember-Tool-Call-Id": "t"})
        self.assertEqual(active["code"], "run_not_found")
        self.assertEqual(len(self.registry.service("default").store.all_events()), 0)

    async def test_duplicate_identity_header_rejected_before_dispatch(self):
        await self.start()
        response, result = await self.tool([("X-Ember-Recall-Id", "r-1"), ("X-Ember-Recall-Id", "r-2"), ("X-Ember-Tool-Call-Id", "t")])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(result["code"], "invalid_identity")

    async def test_duplicate_start_and_bound_end_http_statuses(self):
        first = await self.start()
        duplicate = await self.start()
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json(), first.json())
        finish = await self.client.post("/ember/hooks/turn_end", json={"session_id": "s1", "turn": 1,
            "assistant_reply": "真实回复", "assistant_message_id": "75", "recall_id": "r-1"})
        self.assertEqual(finish.status_code, 200)
        status = await self.client.get("/ember/generations/r-1")
        self.assertEqual(status.json()["status"], "done")
        self.assertTrue(status.json()["recall_snapshot_available"])
        self.assertEqual((await self.client.get("/ember/generations/not-found")).status_code, 404)

    async def test_recall_query_defaults_to_refs_and_response_requires_explicit_view(self):
        first = await self.start()
        refs = await self.client.get("/ember/recalls/r-1")
        self.assertEqual(refs.status_code, 200)
        self.assertNotIn("response", refs.json())
        self.assertNotIn("injected_text", refs.text)
        self.assertEqual(refs.json()["snapshot"]["recall_refs"], first.json()["recall_refs"])
        full = await self.client.get("/ember/recalls/r-1?view=response")
        self.assertEqual(full.json()["response"], first.json())
        self.assertEqual((await self.client.get("/ember/recalls/missing")).status_code, 404)
        self.assertEqual((await self.client.get("/ember/recalls/r-1?view=bad")).status_code, 400)
        self.assertEqual((await self.client.get("/ember/recalls/r-1", headers={"X-Heart-Instance": "second"})).status_code, 404)

    async def test_pending_snapshot_is_queryable_while_original_request_is_running(self):
        import asyncio
        import threading
        entered, release = threading.Event(), threading.Event()
        svc = self.registry.service("default")
        original = svc.run_recall_pipeline
        def delay(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("fixture timeout")
            return original(*args, **kwargs)
        with patch.object(svc, "run_recall_pipeline", side_effect=delay):
            task = asyncio.create_task(self.start())
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                query = await self.client.get("/ember/recalls/r-1")
                self.assertEqual(query.status_code, 202)
                self.assertEqual(query.headers["Retry-After"], "2")
                duplicate = await self.start()
                self.assertEqual(duplicate.status_code, 202)
                self.assertNotIn("recall_refs", duplicate.json())
            finally:
                release.set()
            self.assertEqual((await task).status_code, 200)

    async def test_expired_body_returns_410_and_refs_remain_queryable(self):
        from ember.generations import SNAPSHOT_TTL_SECONDS
        with patch("ember.generations.time.time", return_value=1000):
            await self.start()
        with patch("ember.generations.time.time", return_value=1000 + SNAPSHOT_TTL_SECONDS):
            full = await self.client.get("/ember/recalls/r-1?view=response")
            self.assertEqual(full.status_code, 410)
            self.assertEqual(full.json()["code"], "response_expired")
            duplicate = await self.start()
            self.assertEqual(duplicate.status_code, 410)
            refs = await self.client.get("/ember/recalls/r-1")
            self.assertEqual(refs.status_code, 200)
            self.assertFalse(refs.json()["response_available"])

    async def test_snapshot_route_uses_same_bearer_auth(self):
        await self.start()
        with patch.object(self.http, "TOKEN", "fixture-token"), patch.object(self.http.app, "_expect", b"Bearer fixture-token"):
            self.assertEqual((await self.client.get("/ember/recalls/r-1")).status_code, 401)
            self.assertEqual((await self.client.get("/ember/recalls/r-1", headers={"Authorization": "Bearer fixture-token"})).status_code, 200)

    async def test_snapshot_request_versions_are_passed_and_conflicts_return_409(self):
        body = {"session_id": "s", "message": TEXT, "user_message_id": 71, "recall_id": "versioned",
                "occurred_at": "2026-10-20T08:00:00Z", "activity_revision": 4, "window_revision": 2}
        self.assertEqual((await self.client.post("/ember/hooks/turn_start", json=body)).status_code, 200)
        changed = await self.client.post("/ember/hooks/turn_start", json=dict(body, window_revision=3))
        self.assertEqual(changed.status_code, 409)
        self.assertEqual(changed.json()["code"], "id_conflict")
        refs = (await self.client.get("/ember/recalls/versioned")).json()
        self.assertEqual(refs["snapshot"]["request_context"]["activity_revision"], 4)

    async def test_identified_start_rejects_empty_message_without_registering(self):
        response = await self.client.post("/ember/hooks/turn_start", json={"session_id": "s", "message": "", "user_message_id": 71, "recall_id": "empty"})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(self.registry.service("default").generations.generation("empty"))

    async def test_explicit_null_recall_id_does_not_silently_use_legacy(self):
        response = await self.client.post("/ember/hooks/turn_start", json={"session_id": "s", "message": TEXT, "user_message_id": 71, "recall_id": None})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_identity")


if __name__ == "__main__":
    unittest.main()
