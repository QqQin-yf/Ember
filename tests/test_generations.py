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

    def call(self, rid="r-1", cid="tool-1", args=None, name="create_memory_event"):
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
        self.assertEqual(self.svc.sidecar_call_tool("create_memory_event", write_args())["code"], "identity_required")
        self.assertEqual(self.svc.sidecar_create_memory_event(write_args())["code"], "identity_required")
        self.assertEqual(self.svc.sidecar_call_tool("create_memory_event", write_args(), recall_id="r-1")["code"], "invalid_identity")
        self.assertEqual(self.call(rid="unknown")["code"], "unknown_recall")
        self.assertEqual(len(self.svc.store.all_events()), 0)

    def test_run_or_mixed_identity_does_not_fall_back_to_dialogue(self):
        self.start()
        self.assertEqual(self.svc.sidecar_call_tool("create_memory_event", write_args(), run_id="run1")["code"], "unsupported_phase")
        self.assertEqual(self.svc.sidecar_call_tool("create_memory_event", write_args(), run_id="run1", recall_id="r-1", tool_call_id="t")["code"], "context_mismatch")
        self.assertEqual(len(self.svc.store.all_events()), 0)

    def test_completed_write_retry_survives_supersession_and_restart(self):
        self.start()
        first = self.call()
        self.start("r-2", "s2", 80)
        self.assertEqual(self.call(), first)
        self.assertEqual(self.call(cid="late-new")["code"], "stale_generation")
        again = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        out = again.sidecar_call_tool("create_memory_event", write_args(), recall_id="r-1", tool_call_id="tool-1")
        self.assertEqual(out, first)
        self.assertEqual(len(again.store.all_events()), 1)

    def test_tool_conflicts_are_scoped_to_generation_and_do_not_block_new_calls(self):
        self.start()
        self.call()
        args = write_args()
        args["event"]["core"] = "different memory"
        self.assertEqual(self.call(args=args)["code"], "id_conflict")
        self.assertEqual(self.call(name="pin_memory", args={"event_id": "A"})["code"], "id_conflict")
        self.start("r-2", "s2", 80)
        separate = self.call(rid="r-2")
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
        self.svc.generations.begin_call("r-1", "unfinished", fingerprint({"recall_id": "r-1", "name": "create_memory_event", "arguments": write_args()}), "create_memory_event")
        again = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.assertEqual(again.sidecar_generation_status("r-1")["status"], "interrupted")
        self.assertEqual(again.sidecar_call_tool("create_memory_event", write_args(), recall_id="r-1", tool_call_id="unfinished")["code"], "tool_outcome_unknown")
        self.assertEqual(again.sidecar_call_tool("create_memory_event", write_args(), recall_id="r-1", tool_call_id="new")["code"], "stale_generation")

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
            self.assertEqual(other_svc.sidecar_call_tool("create_memory_event", write_args(), recall_id="r-1", tool_call_id="tool-1")["code"], "unknown_recall")

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

    async def start(self, instance="default", sid="s1", rid="r-1"):
        return await self.client.post("/ember/hooks/turn_start", headers={"X-Heart-Instance": instance},
            json={"session_id": sid, "message": TEXT, "user_message_id": "71", "recall_id": rid})

    async def tool(self, headers=None, args=None):
        response = await self.client.post("/mcp/", headers=headers or {}, json={"jsonrpc": "2.0", "id": 7,
            "method": "tools/call", "params": {"name": "create_memory_event", "arguments": args or write_args()}})
        if response.status_code != 200:
            return response, response.json()
        data = response.json()
        self.assertNotIn("error", data, data)
        return response, json.loads(data["result"]["content"][0]["text"])

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
        self.assertEqual(active["code"], "unsupported_phase")
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
