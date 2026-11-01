"""Actual presentation eligibility and immutable, instance-local provenance."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ember.entity import Entity
from ember.llm import MockLLMClient
from ember.models import Event, PresentationContext, RecallRoute, RecallTrace
from ember.recall import EntityHit
from tests.legacy_event_fixture import LegacyEventService as EmberService
from ember.source_refs import EventSourceStore, SOURCE_REFS_FILE, message_seq
from ember.store import EmberStore
from tests.test_sidecar import write_args


class RecallRefsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        self.fetch = patch.object(self.svc, "_fetched_corpus", return_value=[])
        self.fetch.start()
        self.addCleanup(self.fetch.stop)
        for eid in ("A", "B"):
            self.svc.store.write_event(Event(
                event_id=eid, version_id=eid + "-v1",
                source_ref="conversation/session-old", raw_ref="old",
                event_time="2020-01-01", ingest_time="2020-01-01",
                summary=eid + " summary", core=eid + " body"))
        self.ctx = PresentationContext(recalled_events=["A", "B"],
                                       recall_trace=RecallTrace("query"))
        self.hit = EntityHit(entity_id="ent", name="Project", matched_via="Project")
        self.svc.entity_store.upsert(Entity(entity_id="ent", name="Project",
                                            summary="Entity overview"))

    def own(self, *ids):
        for eid in ids:
            self.svc.store.get_event(eid).entity_refs = ["ent"]

    def render(self, **kwargs):
        items = []
        text = self.svc._build_recall_prefix(self.ctx, presentations=items, **kwargs)
        # Collecting metadata cannot alter the exact model-facing text.
        self.assertEqual(text, self.svc._build_recall_prefix(self.ctx, **kwargs))
        return text, items, self.svc._presented_event_refs(items)

    def test_entity_block_overview_and_expanded_members_are_distinct(self):
        self.own("A", "B")
        text, items, refs = self.render(entity_hits=[self.hit], units=[{
            "kind": "entity_block", "entity_id": "ent", "event_ids": ["A", "B"]}])
        self.assertIn("A body", text)
        self.assertEqual([p["presentation"] for p in items],
                         ["entity_overview", "event_text", "event_text"])
        self.assertEqual([r["event_id"] for r in refs], ["A", "B"])

    def test_light_entity_block_has_no_hidden_event_refs(self):
        self.own("A", "B")
        text, items, refs = self.render(entity_hits=[self.hit], units=[{
            "kind": "entity_block", "entity_id": "ent", "event_ids": ["A", "B"],
            "light": True}])
        self.assertNotIn("A body", text)
        self.assertEqual(refs, [])
        self.assertEqual(items[0]["presentation"], "entity_overview")
        self.assertNotIn("event_id", items[0])

    def test_summary_fallback_does_not_expose_member_ids_as_content(self):
        self.own("A", "B")
        text, items, refs = self.render(entity_hits=[self.hit], summary_fallback=True)
        self.assertIn("A", text)  # IDs are visible but not individual content.
        self.assertEqual(refs, [])
        self.assertTrue(all(p["presentation"] in ("entity_overview", "retrieval_hint")
                            for p in items))

    def test_summary_fallback_for_unowned_event_is_actual_excerpt(self):
        _, _, refs = self.render(summary_fallback=True)
        self.assertEqual([r["presentation"] for r in refs],
                         ["event_excerpt", "event_excerpt"])

    def test_light_member_reference_is_only_an_entity_overview(self):
        self.own("A")
        _, items, refs = self.render(units=[{"kind": "event", "event_ids": ["A"],
                                            "light": True}])
        self.assertEqual(refs, [])
        self.assertEqual(items[0], {"presentation": "entity_overview",
                                    "entity_id": "ent", "light": True})

    def test_light_unowned_event_retains_real_excerpt(self):
        _, items, refs = self.render(units=[{"kind": "event", "event_ids": ["A"],
                                            "light": True}])
        self.assertTrue(items[0]["light"])
        self.assertEqual(refs[0]["presentation"], "event_excerpt")
        self.assertEqual(refs[0]["version_id"], "A-v1")

    def test_event_text_and_retrieval_hint_do_not_add_dropped_events(self):
        _, items, refs = self.render(units=[{"kind": "event", "event_ids": ["A"]}],
                                     strong_dropped=5)
        self.assertEqual([p["presentation"] for p in items],
                         ["event_text", "retrieval_hint"])
        self.assertEqual([r["event_id"] for r in refs], ["A"])

    def test_duplicate_content_is_one_reference_and_keeps_text_granularity(self):
        items = [{"presentation": "event_text", "event_id": "A"},
                 {"presentation": "event_excerpt", "event_id": "A"}]
        refs = self.svc._presented_event_refs(items)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0]["presentation"], "event_text")

    def test_relation_background_does_not_add_superseded_event(self):
        _, _, refs = self.render(units=[{"kind": "event", "event_ids": ["A"]}],
                                  resolved_brought=[("A", "B")])
        self.assertEqual([r["event_id"] for r in refs], ["A"])

    def test_old_ledger_and_birth_turn_do_not_prove_source(self):
        self.svc.turn_spans.fill("A", [(1, 1)], filled_by="test")
        self.svc.turn_ledger.record("old", 1, user_message_id=71)
        self.svc.turn_ledger.record_reply("old", 1, 99999)
        _, _, refs = self.render(units=[{"kind": "event", "event_ids": ["A"]}])
        self.assertEqual(refs[0]["reason"], "source_path_unknown")
        self.assertEqual(refs[0]["turn_messages"], [])
        self.assertIsNone(refs[0]["message_ids"])
        self.assertEqual(refs[0]["source_session_id"], "old")
        self.assertEqual(refs[0]["source_turn_spans"], [[1, 1]])

    def test_blank_event_and_unknown_version_do_not_invent_content_or_version(self):
        event = self.svc.store.get_event("A")
        event.core = event.summary = event.version_id = ""
        _, items, refs = self.render(units=[{"kind": "event", "event_ids": ["A"]}])
        self.assertEqual(refs, [])
        self.assertEqual(items[0]["presentation"], "retrieval_hint")
        self.assertIsNone(items[0]["version_id"])
        event.summary = "A real summary"
        _, _, refs = self.render(units=[{"kind": "event", "event_ids": ["A"]}])
        self.assertEqual(refs[0]["presentation"], "event_text")
        self.assertIsNone(refs[0]["version_id"])

    def test_valid_endpoints_do_not_launder_unknown_middle_corpus(self):
        rows = []
        for turn in (1, 3):
            for role, seq in (("user", turn * 10), ("assistant", turn * 10 + 1)):
                rows.append({"role": role, "turn": turn,
                             "_source_session_id": "old", "_source_message_id": seq})
        rows.append({"role": "assistant", "turn": 2, "content": "unverified"})
        self.svc.event_sources.capture("A", "A-v1", "old", [(1, 3)], rows)
        _, _, refs = self.render(units=[{"kind": "event", "event_ids": ["A"]}])
        self.assertEqual(refs[0]["turn_messages"], [])
        self.assertEqual(refs[0]["source_evidence"], "unverified_corpus")
        self.assertEqual(refs[0]["reason"], "source_path_unknown")

    def test_hook_returns_empty_events_and_no_snapshot_or_extra_turn(self):
        out = self.svc.hook_turn_start("new", "nothing matches", user_message_id=101)
        self.assertFalse(out["injected"])
        self.assertEqual(out["presentations"], [])
        self.assertEqual(out["recall_refs"], {"session_id": "new",
                         "anchor_message_id": "101", "events": []})
        self.assertEqual(out["turn"], 1)
        self.assertNotIn("recall_id", out["recall_refs"])

    def test_hook_returns_only_actual_event_identity_and_source_session(self):
        self.svc.store.write_route(RecallRoute(route_id="route-A", event_id="A",
            matcher="lexical", cue_type="lexical", cue="unique cue"))
        out = self.svc.hook_turn_start('trigger', 'unique cue', user_message_id=104)
        self.assertTrue(out["injected"])
        self.assertEqual(out["recall_refs"]["anchor_message_id"], "104")
        self.assertEqual([r["event_id"] for r in out["recall_refs"]["events"]], ["A"])
        self.assertEqual(out["recall_refs"]["events"][0]["source_session_id"], "old")

    def write_previous_turn(self):
        self.svc.hook_turn_start("source", "今天我们决定把 Ember 改成 Route Search。",
                                 user_message_id=71)
        self.svc.hook_turn_end("source", "已经确认按 Route Search 推进。", turn=1,
                               assistant_message_id=75)
        self.svc.hook_turn_start("source", "请记下刚才的决定。", user_message_id=80)
        args = write_args(quote_spans=[{"start_quote": "今天我们决定把",
                                        "end_quote": "已经确认按 Route Search 推进。"}])
        # Simulate provider-proven historical corpus; legacy max-reply mappings
        # themselves are still deliberately not accepted as historical identity.
        self.fetch.stop()
        proof = patch.object(self.svc, "_fetched_corpus", return_value=list(self.svc.sessions["source"].messages))
        proof.start()
        self.addCleanup(proof.stop)
        out = self.svc.sidecar_create_memory_event(args)
        self.assertEqual(out["status"], "created", out)
        return out["event_id"]

    def test_new_event_freezes_exact_messages_across_ledger_changes_and_restart(self):
        eid = self.write_previous_turn()
        original = self.svc.event_sources.get(eid)
        self.assertEqual(original["source_turn_spans"], [[1, 1]])
        self.assertEqual(original["turn_messages"], [{"turn": 1,
            "user_message_id": "71", "assistant_message_id": "75"}])
        self.svc.turn_ledger.record_reply("source", 1, 999)
        self.svc.turn_spans.fill(eid, [(5, 9)], filled_by="changed")
        again = EmberService(EmberStore(self.tmp.name), MockLLMClient())
        refs = again._presented_event_refs([{"presentation": "event_text", "event_id": eid}])
        self.assertEqual(refs[0]["turn_messages"], original["turn_messages"])
        self.assertEqual(refs[0]["source_turn_spans"], [[1, 1]])
        self.assertEqual(refs[0]["resolution"], "unresolved")  # app still verifies path.
        self.assertEqual(refs[0]["source_evidence"], "frozen_messages")
        with open(Path(self.tmp.name) / SOURCE_REFS_FILE, encoding="utf-8") as f:
            raw = f.read()
        self.assertNotIn("今天", raw)
        self.assertNotIn("content", raw)
        self.assertNotIn("turn_messages", again.store.get_event(eid).to_dict())
        self.assertNotIn("source_turn_spans", again.store.get_event(eid).to_dict())

    def test_in_flight_reply_is_not_backfilled_from_later_turn_end(self):
        self.svc.hook_turn_start("source", "今天我们决定把 Ember 改成 Route Search。",
                                 user_message_id=71)
        out = self.svc.sidecar_create_memory_event(write_args())
        self.assertEqual(out["status"], "created", out)
        eid = out["event_id"]
        self.svc.hook_turn_end("source", "后来的回复不是写入时的证据", turn=1,
                               assistant_message_id=75)
        ref = self.svc._presented_event_refs([{"presentation": "event_text", "event_id": eid}])[0]
        self.assertEqual(ref["reason"], "source_seq_missing")
        self.assertIsNone(ref["turn_messages"][0]["assistant_message_id"])

    def test_failed_quote_validation_does_not_create_event_or_source_record(self):
        self.svc.hook_turn_start("source", "other text", user_message_id=71)
        before = len(self.svc.store.all_events())
        out = self.svc.sidecar_create_memory_event(write_args())
        self.assertEqual(out["status"], "failed")
        self.assertEqual(len(self.svc.store.all_events()), before)
        self.assertFalse((Path(self.tmp.name) / SOURCE_REFS_FILE).exists())


class SourceStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sources = EventSourceStore(self.tmp.name)

    def test_legacy_or_cross_session_corpus_does_not_gain_identity(self):
        for row in ({"role": "user", "turn": 1, "message_id": 71},
                    {"role": "user", "turn": 1, "_source_message_id": "71",
                     "_source_session_id": "another-session"}):
            self.sources.capture(str(row), "v1", "s", [(1, 1)], [row])
            frozen = self.sources.get(str(row))
            self.assertIsNone(frozen["turn_messages"][0]["user_message_id"])
            self.assertFalse(frozen["identity_verified"])

    def test_first_record_wins_and_get_does_not_expose_mutable_state(self):
        self.sources.capture("A", "v1", "s", [(1, 1)], [])
        self.sources.capture("A", "v2", "different", [(2, 2)], [])
        one = self.sources.get("A")
        one["turn_messages"].clear()
        again = EventSourceStore(self.tmp.name).get("A")
        self.assertEqual(again["version_id"], "v1")
        self.assertEqual(len(again["turn_messages"]), 1)
        with open(self.sources.path, encoding="utf-8") as stream:
            self.assertEqual(len(stream.readlines()), 1)

    def test_instances_do_not_share_event_sources(self):
        with tempfile.TemporaryDirectory() as other:
            self.sources.capture("A", "v1", "s", [(1, 1)], [])
            self.assertIsNone(EventSourceStore(other).get("A"))

    def test_seq_validation_excludes_uuid_bool_and_invalid_ids(self):
        for value in (None, True, False, 0, -1, "uuid", "", "-1", "１"):
            self.assertIsNone(message_seq(value))
        self.assertEqual(message_seq(71), "71")
        self.assertEqual(message_seq("75"), "75")


if __name__ == "__main__":
    unittest.main()
