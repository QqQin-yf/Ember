"""Exact wire identities, fail-closed responses and all source consumers."""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from ember import source_client
from ember.source_paths import read_messages, generation_messages
from ember.service import EmberService
from ember.store import EmberStore
from ember.llm import MockLLMClient
from ember.models import Event

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append((self.path, dict(self.headers)))
        self.send_response(self.server.status)
        self.end_headers()
        self.wfile.write(json.dumps(self.server.payload).encode())
    def log_message(self, *args):
        pass

class ExactPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests, self.server.status = [], 200
        self.server.payload = {"messages": [{"message_id": "71", "role": "user", "content": "old user"},
                                            {"message_id": "75", "role": "assistant", "content": "old assistant"}]}
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        config = patch.multiple(source_client, SOURCE_URL=f"http://127.0.0.1:{self.server.server_port}", SOURCE_TOKEN="synthetic-test-token")
        config.start()
        self.addCleanup(config.stop)

    def event(self, *, evidence=True, version="v1", spans=None):
        event = Event(event_id="E", version_id=version, source_ref="conversation/session-source",
                      raw_ref="r", event_time="2026-10-21", ingest_time="2026-10-21", summary="s", core="c")
        self.svc.store.write_event(event)
        self.svc.turn_spans.fill("E", spans or [(1, 1)], filled_by="test")
        if evidence:
            self.svc.event_sources.capture("E", "v1", "source", spans or [(1, 1)], [
                {"turn": 1, "role": role, "_source_session_id": "source", "_source_message_id": seq}
                for role, seq in (("user", "71"), ("assistant", "75"))])

    def test_exact_query_auth_instance_and_string_ids(self):
        rows = source_client.fetch_source_path("source", [75, "71"], instance="phase2")
        self.assertEqual([r["message_id"] for r in rows], [71, 75])
        url, headers = self.server.requests[-1]
        self.assertEqual(url, "/api/source/path?session_id=source&message_ids=71%2C75")
        self.assertEqual(headers["Authorization"], "Bearer synthetic-test-token")
        self.assertEqual(headers["X-Heart-Instance"], "phase2")

    def test_bad_or_partial_identity_response_rejected_as_a_whole(self):
        for rows in ([], self.server.payload["messages"][:1], list(reversed(self.server.payload["messages"])),
                     [{"message_id": 71, "role": "user", "content": "x"}] * 2,
                     [{"message_id": 71, "role": "tool", "content": "x"}, self.server.payload["messages"][1]]):
            with self.subTest(rows=rows):
                self.server.payload = {"messages": rows}
                with self.assertRaises(source_client.SourceUnavailable):
                    source_client.fetch_source_path("source", [71, 75])

    def test_rejected_http_never_falls_back_to_range(self):
        self.event()
        for status in (401, 404, 409, 500):
            self.server.status = status
            self.assertEqual(self.svc.read_source_event("E")["status"], "not_archived")
        self.assertEqual(len(self.server.requests), 4)
        self.assertTrue(all(p.startswith("/api/source/path?") for p, _ in self.server.requests))

    def test_frozen_event_ignores_current_memory_and_max_ledger(self):
        self.event()
        self.svc.hook_turn_start("source", "wrong current branch", user_message_id=800)
        self.svc.turn_ledger.record_reply("source", 1, 999)
        self.svc.turn_spans.fill("E", [(3, 9)], filled_by="changed")
        result = self.svc.read_source_event("E")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["turns"], [1])
        self.assertFalse(result["context_expanded"])
        self.assertIn("old assistant", result["transcript"])
        self.assertNotIn("wrong current branch", result["transcript"])
        lines = []
        self.assertTrue(self.svc._inline_source_lines("E", lines))
        self.assertIn("old assistant", self.svc._briefing_source_block(self.svc.store.get_event("E")))

    def test_legacy_or_changed_version_never_guesses(self):
        for evidence, version in ((False, "v1"), (True, "v2")):
            self.event(evidence=evidence, version=version)
            self.assertEqual(self.svc.read_source_event("E")["status"], "not_archived")
        self.assertEqual(self.server.requests, [])

    def test_missing_middle_turn_or_reply_does_not_return_partial_source(self):
        self.event(spans=[(1, 2)])
        self.assertEqual(self.svc.read_source_event("E")["reason"], "source_seq_missing")
        self.assertFalse(self.server.requests)

    def test_legacy_mapping_alone_is_not_remote_evidence(self):
        self.svc.turn_ledger.record("source", 1, user_message_id=71)
        self.svc.turn_ledger.record_reply("source", 1, 75)
        self.assertEqual(self.svc._fetched_corpus("source"), [])
        self.assertFalse(self.server.requests)
        with self.assertRaises(source_client.SourceUnavailable):
            self.svc._observe_session("source")
        self.assertEqual(self.svc.observe_cursor.get("source"), 0)

    def test_rejected_path_does_not_launder_local_history_into_quote_corpus(self):
        self.svc.hook_turn_start("source", "old user", user_message_id=71, recall_id="old")
        self.svc.hook_turn_end("source", "old assistant", turn=1, assistant_message_id=75, recall_id="old")
        self.svc.hook_turn_start("source", "new user", user_message_id=80, recall_id="new")
        self.server.status = 409
        corpus = self.svc._quote_corpus(self.svc.sessions["source"])
        self.assertEqual([m["content"] for m in corpus], ["new user"])
        expected = generation_messages(self.svc.generations, "source")
        self.assertEqual([m["message_id"] for m in expected], ["71", "75", "80"])
        self.assertIn("message_ids=71%2C75%2C80", self.server.requests[-1][0])

    def test_role_conflict_is_rejected(self):
        with self.assertRaises(source_client.SourceUnavailable):
            read_messages("source", [{"message_id": "71", "turn": 1, "role": "assistant"},
                                     {"message_id": "75", "turn": 1, "role": "assistant"}], "default")

    def test_invalid_inputs_fail_before_network(self):
        for ids in ([], [True], [0], ["uuid"], [71, "71"]):
            with self.assertRaises(source_client.SourceUnavailable):
                source_client.fetch_source_path("source", ids)
        self.assertFalse(self.server.requests)
