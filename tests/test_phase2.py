"""Hooks/MCP lifecycle checks migrated from the retired proxy acceptance suite."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from ember.llm import MockLLMClient
from ember.recall import DirectRecall
from tests.legacy_event_fixture import LegacyEventService as EmberService
from ember.store import EmberStore
from tests.test_sidecar import write_args

class HookMemoryLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.client = MockLLMClient()
        self.svc = EmberService(EmberStore(self.tmp.name), self.client)
        no_source = patch.object(self.svc, "_fetched_corpus", return_value=[])
        no_source.start()
        self.addCleanup(no_source.stop)

    def start(self, text="今天我们决定把 Ember 的传播模型改成 Route Search。"):
        return self.svc.hook_turn_start("s", text, user_message_id=1)

    def write(self):
        return self.svc.sidecar_call_tool("create_memory_event", write_args())

    def test_hooks_do_not_call_model_or_write_implicitly(self):
        self.start()
        self.assertFalse(self.svc.store.all_events())
        self.assertTrue(self.svc.hook_turn_end("s", "确认。", assistant_message_id=2)["ok"])
        self.assertEqual(self.client.calls, [])
        self.assertFalse(self.svc.store.all_events())

    def test_explicit_tool_generates_system_fields_and_routes(self):
        self.start()
        result = self.write()
        self.assertEqual(result["status"], "created")
        event = self.svc.store.get_event(result["event_id"])
        self.assertTrue(event.version_id.endswith("-v1"))
        self.assertEqual(event.raw_ref, "conversation/session-s/turn-1")
        self.assertEqual(event.turn_refs, ["turn-1"])
        self.assertEqual(event.change, "")
        self.assertNotIn("activation", event.to_dict())
        self.assertGreaterEqual(result["route_count"], 1)

    def test_recall_returned_separately_from_user_history(self):
        self.start()
        result = self.write()
        self.svc.hook_turn_end("s", "确认。", assistant_message_id=2)
        recalled = self.svc.hook_turn_start("s", "我们继续聊 Ember 吧。", user_message_id=3)
        self.assertTrue(recalled["injected"])
        self.assertIn("[记忆浮现]", recalled["injected_text"])
        self.assertIn("触点：", recalled["injected_text"])
        self.assertEqual(self.svc.sessions["s"].messages[-1]["content"], "我们继续聊 Ember 吧。")
        presentation = self.svc.recall("Ember")
        self.assertTrue(any(row["event"]["event_id"] == result["event_id"] for row in presentation["recalled_events"]))
        self.assertTrue(all("route_id" in why for why in presentation["recalled_events"][0]["why_recalled"]))

    def test_validation_failure_does_not_write_or_block_chat(self):
        self.start()
        result = self.svc.sidecar_call_tool("create_memory_event", {"event": {"summary": "残缺"}})
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["error"])
        self.assertFalse(self.svc.store.all_events())
        self.assertTrue(self.svc.hook_turn_end("s", "普通聊天可以继续。", assistant_message_id=2)["ok"])

    def test_restart_keeps_index_and_counter_without_body_archive(self):
        self.start()
        result = self.write()
        self.svc.hook_turn_end("s", "确认。", assistant_message_id=2)
        restored = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.assertEqual(restored.store.index_snapshot(), self.svc.store.index_snapshot())
        self.assertEqual(restored.store.get_event(result["event_id"]).raw_ref, "conversation/session-s/turn-1")
        session = restored.get_or_create_session("s")
        self.assertEqual(session.turn_counter, 1)
        self.assertEqual(session.messages, [])
        self.assertEqual(restored.hook_turn_start("s", "重启后一句。", user_message_id=3)["turn"], 2)
        self.assertFalse((Path(self.tmp.name) / "transcripts.jsonl").exists())

    def test_rejected_key_quote_does_not_reappear_after_reload(self):
        self.start()
        result = self.svc.sidecar_call_tool("create_memory_event", write_args(
            user_quote="今天我们决定把", extra_ai_quote="这句 AI 从未说过"))
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["dropped_quotes"], [{"speaker": "ai", "text": "这句 AI 从未说过"}])
        restored = EmberStore(self.tmp.name).get_event(result["event_id"])
        self.assertEqual(restored.key_quotes, [{"speaker": "user", "text": "今天我们决定把"}])

    def test_multiple_cues_aggregate_without_composite_phrase(self):
        text = "我决定记下这件事：堂姐结婚，国庆办，在河南。"
        self.start(text)
        args = write_args(cues=("堂姐结婚", "堂姐", "结婚", "国庆", "河南"),
                          quote_spans=[{"start_quote": text, "end_quote": text}])
        result = self.svc.sidecar_call_tool("create_memory_event", args)
        self.assertEqual(result["status"], "created")
        for query, count in (("这次国庆没人结婚。", 2), ("河南最近怎么样。", 1)):
            with self.subTest(query=query):
                found = next(row for row in self.svc.recall(query)["recalled_events"]
                             if row["event"]["event_id"] == result["event_id"])
                self.assertEqual(found["multi_cue"], count)
        prefix = self.svc._build_recall_prefix(DirectRecall(self.svc.store).recall("这次国庆没人结婚。"))
        self.assertIn("多个线索同时指向", prefix)

if __name__ == "__main__":
    unittest.main()
