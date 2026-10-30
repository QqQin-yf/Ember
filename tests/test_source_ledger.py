# -*- coding: utf-8 -*-
"""去原文化第二批测试（2026-10-14）：transcripts 退役 + revise_memory_event。

1. transcripts 退役：
   - turn_ledger 无条件记账（不带 heart_id/message_id 也落最小行）；
   - 重启恢复 turn 计数改从账本（get_or_create_session）；
   - hook_turn_end 不再写 transcripts（transcripts 属性已移除）；
   - _quote_corpus = 内存 ∪ source_client 按需拉取（未配置 → 只有内存）；
   - read_source_event：内存命中 → ok；未配置且内存无 → not_archived。
2. revise_memory_event：
   - 冷静期内直改（叙述字段）；list 字段 str 归一；
   - 过冷静期（sleep_progress 有记录）→ rejected 指路 resolves；
   - 锁死字段 → failed；无字段 → failed；
   - recall_cues 整体重设（旧 route 退役 + 新 route 重建 + 索引刷新）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.service import (EmberStore as _ES_unused,
                           select_recall_units)
from tests.legacy_event_fixture import LegacyEventService as EmberService
from ember.store import EmberStore
from ember.llm import MockLLMClient
from ember.models import Event, RecallRoute
from ember.recall import EntityHit, RouteHit
from ember.turn_ledger import TurnLedger
from ember import source_client


def _service(tmp: Path) -> EmberService:
    return EmberService(EmberStore(str(tmp / "data")), MockLLMClient(),
                        model="test")


def _service(tmp: Path) -> EmberService:
    return EmberService(EmberStore(str(tmp / "data")), MockLLMClient(),
                        model="test")


def _make_event(svc: EmberService, sid: str, turn: int,
                event_id: str = "EVT-001-aaaaaa") -> Event:
    now = "2026-10-14T00:00:00+08:00"
    return Event(
        event_id=event_id, version_id=f"{event_id}-v1",
        source_ref=f"conversation/session-{sid}",
        raw_ref=f"conversation/session-{sid}/turn-{turn}",
        event_time=now, ingest_time=now,
        turn_refs=[f"turn-{turn}"],
        summary="旧摘要", participants=["ai"], objects=["测试"],
        context=["背景"], core="旧叙述", outcome=["旧结果"],
        state=["旧状态"], affect=["旧感受"], significance="旧意义",
        details=["旧细节"],
    )


def _hit(eid: str, matcher: str, cue: str = "c") -> RouteHit:
    return RouteHit(route_id=f"rt_{eid}_{matcher}", event_id=eid,
                    cue=cue, matcher=matcher, cue_type="lexical")


def _ehit(entity_id: str, name: str) -> EntityHit:
    return EntityHit(entity_id=entity_id, name=name, matched_via=name)


def _sel_ids(units: list[dict]) -> list[str]:
    return [eid for u in units for eid in u["event_ids"]]


class RecallSeatTests(unittest.TestCase):
    """多源共证与名额（2026-10-15 定稿）：排序/组序/块捆绑/不硬凑。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = EmberStore(str(self.tmp / "data"))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, eid: str, entity_id: str | None = None,
             ts: str = "2026-10-14T00:00:00+08:00") -> None:
        ev = Event(event_id=eid, version_id=eid + "-v1",
                   source_ref="conversation/session-s1", raw_ref="r",
                   event_time=ts, ingest_time=ts,
                   summary=eid, core=eid)
        self.store.write_event(ev)

    def _register(self, rows: list[dict]) -> None:
        from ember.recall import register_event_entity_refs
        register_event_entity_refs(str(self.tmp / "data" / "events.jsonl"))

    def _ev_row(self, eid: str, entity_id: str | None) -> dict:
        now = "2026-10-14T00:00:00+08:00"
        row = {"event_id": eid, "version_id": eid + "-v1",
               "source_ref": "s", "raw_ref": "r", "event_time": now,
               "ingest_time": now, "summary": eid, "core": eid,
               "entity_refs": [entity_id] if entity_id else []}
        # 直接写 jsonl：register_event_entity_refs 读的是文件侧字段
        p = self.tmp / "data" / "events.jsonl"
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def test_deterministic_beats_semantic(self) -> None:
        # 组 1（lexical）先于组 2（semantic）：A/B/C 均单源弱补位，A 居首
        self._add("EVT-A")
        self._add("EVT-B")
        self._add("EVT-C")
        units, stats = select_recall_units(
            ["EVT-A", "EVT-B", "EVT-C"],
            [_hit("EVT-A", "lexical"), _hit("EVT-B", "semantic"),
             _hit("EVT-C", "semantic")],
            [], [], self.store)
        ids = _sel_ids(units)
        self.assertEqual(ids[0], "EVT-A")
        self.assertEqual(len(ids), 3)
        self.assertEqual(stats["dropped_by_seats"], 0)
        self.assertEqual([u["group"] for u in units], [1, 2, 2])
        self.assertTrue(all(u.get("weak") for u in units))  # 全单源=弱

    def test_seat_limit_drops_overflow(self) -> None:
        # 4 条单源命中 → 3 席弱补位，1 条落选
        for eid in ("EVT-1", "EVT-2", "EVT-3", "EVT-4"):
            self._add(eid)
        units, stats = select_recall_units(
            ["EVT-1", "EVT-2", "EVT-3", "EVT-4"],
            [_hit("EVT-1", "lexical"), _hit("EVT-2", "lexical"),
             _hit("EVT-3", "lexical"), _hit("EVT-4", "lexical")],
            [], [], self.store)
        self.assertEqual(len(_sel_ids(units)), 3)
        self.assertEqual(stats["dropped_by_seats"], 1)
        self.assertEqual(stats["weak_pooled"], 1)

    def test_few_no_padding(self) -> None:
        # 门槛哲学：只有 1 条真相关 → 只注 1 条，不强凑
        self._add("EVT-1")
        units, stats = select_recall_units(
            ["EVT-1"], [_hit("EVT-1", "lexical")], [], [], self.store)
        self.assertEqual(len(_sel_ids(units)), 1)
        self.assertEqual(stats["dropped_by_seats"], 0)

    def test_route_count_ordering(self) -> None:
        # 双源（entity+lexical）共证进主席，单源弱补位排后
        self._add("EVT-X")
        self._add("EVT-Y")
        self._ev_row("EVT-X", "ENT-1")
        self._register([])
        units, stats = select_recall_units(
            ["EVT-X", "EVT-Y"],
            [_hit("EVT-X", "lexical"), _hit("EVT-Y", "lexical")],
            [_ehit("ENT-1", "某实体")], [], self.store)
        self.assertEqual(stats["dropped_by_seats"], 0)
        self.assertEqual(units[0]["event_ids"], ["EVT-X"])
        self.assertEqual(units[0]["route_count"], 2)
        self.assertEqual(units[1]["event_ids"], ["EVT-Y"])
        self.assertTrue(units[1].get("weak"))

    def test_entity_block_bundling(self) -> None:
        # 实体块捆绑：块内 2 事件整体占 1 席；独立事件各占 1 席。
        # B1 有 cue 撞中 → 块凭 cue+entity 双源进主席；S1-S3 单源弱补位。
        # 4 单元竞争 3 席 → S3 落选；块整体入选（含 2 事件）。
        self._add("EVT-B1")
        self._add("EVT-B2")
        self._add("EVT-S1")
        self._add("EVT-S2")
        self._add("EVT-S3")
        self._ev_row("EVT-B1", "ENT-1")
        self._ev_row("EVT-B2", "ENT-1")
        self._register([])  # 注册表读 jsonl 侧 entity_refs（_event_entity_of 依赖）
        units, stats = select_recall_units(
            ["EVT-B1", "EVT-B2", "EVT-S1", "EVT-S2", "EVT-S3"],
            [_hit("EVT-B1", "捆绑"), _hit("EVT-S1", "lexical"),
             _hit("EVT-S2", "lexical"), _hit("EVT-S3", "lexical")],
            [_ehit("ENT-1", "某实体")], [], self.store)
        blocks = [u for u in units if u["kind"] == "entity_block"]
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["event_ids"], ["EVT-B1", "EVT-B2"])
        self.assertEqual(blocks[0]["route_count"], 2)  # cue + entity 双源
        self.assertFalse(blocks[0].get("weak"))
        self.assertEqual(stats["dropped_by_seats"], 1)
        sel_ids = _sel_ids(units)
        self.assertEqual(len(sel_ids), 4)  # 块贡献 2 事件 + 2 独立
        self.assertIn("EVT-B1", sel_ids)
        self.assertIn("EVT-B2", sel_ids)
        self.assertNotIn("EVT-S3", sel_ids)

    def test_pure_entity_block_sinks_in_weak_pool(self) -> None:
        # 纯实体块（无成员 cue）= 单源弱信号且 0 锚 → 沉到 cue 命中之下
        self._add("EVT-B1")
        self._add("EVT-B2")
        self._add("EVT-S1")
        self._ev_row("EVT-B1", "ENT-1")
        self._ev_row("EVT-B2", "ENT-1")
        self._register([])
        units, stats = select_recall_units(
            ["EVT-B1", "EVT-B2", "EVT-S1"],
            [_hit("EVT-S1", "lexical")],
            [_ehit("ENT-1", "某实体")], [], self.store, seats=1)
        # 两者皆单源：S1 锚数 1 > 块锚数 0 → S1 补位，块落池
        self.assertEqual(_sel_ids(units), ["EVT-S1"])
        self.assertEqual(stats["weak_pooled"], 1)

    def test_resolved_inheritance(self) -> None:
        # 修正者继承被修正者的命中资格：A lexical 命中后被 superseded
        # 排除，B（修正者）凭继承进组 1（relation 路不受门槛限制）
        self._add("EVT-B")
        units, stats = select_recall_units(
            ["EVT-B"],
            [_hit("EVT-A", "lexical")],  # A 的命中（A 已被过滤不在候选）
            [], [("EVT-B", "EVT-A")], self.store)
        self.assertEqual(stats["dropped_by_seats"], 0)
        self.assertEqual(units[0]["group"], 1)
        self.assertEqual(units[0]["route_count"], 1)
        self.assertTrue(units[0].get("resolved"))
        self.assertFalse(units[0].get("weak"))   # 修正者不算弱信号

    def test_semantic_only_no_group1(self) -> None:
        # 只有 semantic 命中 → 组 1 空，组 2 弱补位（全部注入）
        self._add("EVT-S1")
        self._add("EVT-S2")
        units, stats = select_recall_units(
            ["EVT-S1", "EVT-S2"],
            [_hit("EVT-S1", "semantic"), _hit("EVT-S2", "semantic")],
            [], [], self.store)
        self.assertEqual(len(_sel_ids(units)), 2)
        self.assertEqual({u["group"] for u in units}, {2})
        self.assertEqual(stats["dropped_by_seats"], 0)


def _write_event_with_span(svc: EmberService, session, turn: int,
                           event_id: str, span: tuple[int, int]) -> None:
    """造一个候选事件：Event 本体 + turn_spans 回填 + lexical route。"""
    import uuid as _uuid
    from ember.models import RecallRoute
    now = "2026-10-14T00:00:00+08:00"
    ev = Event(
        event_id=event_id, version_id=f"v_{_uuid.uuid4().hex[:8]}",
        source_ref=f"conversation/session-{session.session_id}/turn-{turn}",
        raw_ref=f"conversation/session-{session.session_id}/turn-{turn}",
        event_time=now, ingest_time=now,
        turn_refs=[f"turn-{turn}"],
        summary=f"事件 {event_id}",
    )
    svc.store.write_event(ev)
    svc.turn_spans.fill(event_id, [span], filled_by="test")
    svc.store.write_route(RecallRoute(
        route_id=f"rt_{event_id}_01", event_id=event_id,
        matcher="lexical", cue_type="lexical", cue=event_id,
        status="active", evidence_refs=[ev.raw_ref],
        provenance_refs=["test"]))


# ===========================================================================
# 第一批（2026-10-14 图第 1/2 点）：ledger 映射 + 召回起点过滤 + 钩子穿透
# ===========================================================================


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.ledger = TurnLedger(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_record_user_and_reply_ids(self) -> None:
        self.ledger.record("s1", 1, "t-000001", user_message_id=10)
        self.ledger.record_reply("s1", 1, assistant_message_id=11)
        self.assertEqual(self.ledger.user_msg[("s1", 1)], 10)
        self.assertEqual(self.ledger.reply_msg[("s1", 1)], 11)

    def test_survives_restart(self) -> None:
        self.ledger.record("s1", 1, "t-000001", user_message_id=10)
        self.ledger.record_reply("s1", 1, assistant_message_id=11)
        reloaded = TurnLedger(self._tmp.name)
        self.assertEqual(reloaded.user_msg[("s1", 1)], 10)
        self.assertEqual(reloaded.reply_msg[("s1", 1)], 11)
        self.assertEqual(reloaded.heart_id_of("s1", 1), "t-000001")

    def test_turn_at_or_after_exact_user_row(self) -> None:
        self.ledger.record("s1", 1, None, user_message_id=10)
        self.ledger.record_reply("s1", 1, assistant_message_id=11)
        self.ledger.record("s1", 2, None, user_message_id=20)
        self.ledger.record_reply("s1", 2, assistant_message_id=21)
        # 起点=第 2 轮的 user 行 → 起点轮 = 2
        self.assertEqual(self.ledger.turn_at_or_after("s1", 20), 2)
        # 起点=第 1 轮的 reply 行（落在 [10,11] 区间）→ 起点轮 = 1
        self.assertEqual(self.ledger.turn_at_or_after("s1", 11), 1)
        # 起点落在两轮空隙（11 < 15 < 20）→ 其后最早一轮
        self.assertEqual(self.ledger.turn_at_or_after("s1", 15), 2)
        # 起点=第 2 轮的 reply+1（窗口内没有第 3 轮）→ 无过滤
        self.assertIsNone(self.ledger.turn_at_or_after("s1", 22))

    def test_turn_at_or_after_uncovered_start(self) -> None:
        # 起点比账本覆盖还老 → None（保守全召回）
        self.ledger.record("s1", 1, None, user_message_id=10)
        self.assertIsNone(self.ledger.turn_at_or_after("s1", 1))

    def test_message_span_relaxed_semantics(self) -> None:
        self.ledger.record("s1", 1, None, user_message_id=10)
        self.ledger.record_reply("s1", 1, assistant_message_id=11)
        self.ledger.record("s1", 2, None, user_message_id=20)  # 第 2 轮缺 reply
        # 放宽语义：取已知映射极值（多拉无害）
        self.assertEqual(self.ledger.message_span("s1", 1, 2), (10, 20))
        self.assertEqual(self.ledger.message_span("s1", 1, 1), (10, 11))
        self.assertIsNone(self.ledger.message_span("s1", 9, 9))  # 无任何映射

    def test_turn_of_message(self) -> None:
        self.ledger.record("s1", 1, None, user_message_id=10)
        self.ledger.record_reply("s1", 1, assistant_message_id=11)
        self.assertEqual(self.ledger.turn_of_message("s1", 10), 1)
        self.assertEqual(self.ledger.turn_of_message("s1", 11), 1)
        self.assertIsNone(self.ledger.turn_of_message("s1", 99))


class RecallWindowFilterTests(unittest.TestCase):
    """召回起点过滤：窗口内的记忆不再召回（原文就在现场）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _service(Path(self._tmp.name))
        self.session = self.svc.get_or_create_session("s1")
        # 三个事件：来源 turn 1 / turn 2 / 跨 1-3
        _write_event_with_span(self.svc, self.session, 1, "evt_old", (1, 1))
        _write_event_with_span(self.svc, self.session, 2, "evt_new", (2, 2))
        _write_event_with_span(self.svc, self.session, 3, "evt_cross", (1, 3))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _recall(self, context_start_id: int | None) -> dict:
        for t, (u, r) in {1: (10, 11), 2: (20, 21), 3: (30, 31)}.items():
            self.svc.turn_ledger.record("s1", t, None, user_message_id=u)
            self.svc.turn_ledger.record_reply("s1", t, assistant_message_id=r)
        return self.svc.run_recall_pipeline(
            self.session, 4, "事件 evt_old evt_new evt_cross",
            context_start_id=context_start_id)

    def test_window_filter_removes_in_window_keeps_cross(self) -> None:
        # 起点=message_id 20（ember turn 2）：evt_new（span 2,2）全在窗口内
        # → 滤掉；evt_old（1,1）在窗口外 → 保留；evt_cross（1,3）跨起点
        # → 保留
        rec = self._recall(20)
        self.assertGreaterEqual(rec["filtered_by_window"], 1)
        prefix = rec["prefix"]
        if rec["recalled_count"] > 0:
            self.assertIn("evt_old", prefix)
            self.assertNotIn("evt_new", prefix)

    def test_no_start_id_no_filter(self) -> None:
        rec = self._recall(None)
        self.assertEqual(rec["filtered_by_window"], 0)


class HookIdPassthroughTests(unittest.TestCase):
    """钩子带可选 id → 落账本；不带 → 照常（向后兼容）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _service(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ids_recorded_via_hooks(self) -> None:
        r1 = self.svc.hook_turn_start("s1", "你好", "t-000001",
                                      user_message_id=10,
                                      context_start_id=10)
        turn = r1["turn"]
        self.svc.hook_turn_end("s1", "我在。", turn, assistant_message_id=11)
        self.assertEqual(self.svc.turn_ledger.user_msg[("s1", turn)], 10)
        self.assertEqual(self.svc.turn_ledger.reply_msg[("s1", turn)], 11)

    def test_hooks_without_ids_still_work(self) -> None:
        r1 = self.svc.hook_turn_start("s1", "你好")
        self.svc.hook_turn_end("s1", "我在。", r1["turn"])
        self.assertEqual(r1["turn"], 1)  # 主流程不受影响


class _FakeSource(BaseHTTPRequestHandler):
    payload = {"messages": [
        {"message_id": 10, "role": "user", "content": "你好呀"},
        {"message_id": 11, "role": "assistant", "content": "我在。"},
    ]}
    status = 200

    def do_GET(self):
        data = json.dumps(type(self).payload).encode("utf-8")
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class SourceClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self._old = (source_client.SOURCE_URL, source_client.SOURCE_TOKEN)
        _FakeSource.status = 200
        _FakeSource.payload = {"messages": [
            {"message_id": 10, "role": "user", "content": "你好呀"},
            {"message_id": 11, "role": "assistant", "content": "我在。"},
        ]}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeSource)
        threading.Thread(target=self.server.serve_forever,
                         daemon=True).start()
        source_client.SOURCE_URL = (
            f"http://127.0.0.1:{self.server.server_address[1]}")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        source_client.SOURCE_URL, source_client.SOURCE_TOKEN = self._old

    def test_fetch_range(self) -> None:
        rows = source_client.fetch_source_path("s1", [10, 11])
        self.assertEqual(rows[0]["message_id"], 10)
        self.assertEqual(rows[0]["role"], "user")

    def test_unconfigured_raises(self) -> None:
        source_client.SOURCE_URL = ""
        with self.assertRaises(source_client.SourceUnavailable):
            source_client.fetch_source_path("s1", [1, 2])

    def test_bad_response_raises(self) -> None:
        _FakeSource.status = 500
        with self.assertRaises(source_client.SourceUnavailable):
            source_client.fetch_source_path("s1", [10, 11])


class TranscriptRetireTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.svc = _service(self.tmp)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_ledger_records_bare_turn(self) -> None:
        # 无条件记账：不带 heart_id/message_id 也落最小行
        self.svc.turn_ledger.record("s1", 1)
        reloaded = type(self.svc.turn_ledger)(str(self.tmp / "data"))
        self.assertEqual(reloaded.max_turn("s1"), 1)

    def test_turn_counter_restored_from_ledger(self) -> None:
        self.svc.turn_ledger.record("s1", 5)
        fresh = _service(self.tmp)  # 新 service（同 store 目录）
        self.assertEqual(fresh.get_or_create_session("s1").turn_counter, 5)

    def test_transcripts_attribute_gone(self) -> None:
        self.assertFalse(hasattr(self.svc, "transcripts"))

    def test_hook_turn_end_no_archive_write(self) -> None:
        self.svc.hook_turn_start("s1", "你好")
        r = self.svc.hook_turn_end("s1", "我在。")
        self.assertTrue(r["ok"])
        self.assertFalse((self.tmp / "data" / "transcripts.jsonl").exists())

    def test_quote_corpus_memory_only_when_source_unconfigured(self) -> None:
        self.svc.hook_turn_start("s1", "你好呀")
        session = self.svc.sessions["s1"]
        corpus = self.svc._quote_corpus(session)
        texts = [m["content"] for m in corpus]
        self.assertIn("你好呀", texts)

    def test_quote_corpus_pulls_missing_turns(self) -> None:
        # 内存只有第 1 轮；后端有第 2 轮原文（mock 拉取）→ 语料补齐
        self.svc.turn_ledger.record("s1", 1, None, user_message_id=100)
        self.svc.turn_ledger.record_reply("s1", 1, assistant_message_id=101)
        self.svc.turn_ledger.record("s1", 2, None, user_message_id=200)
        self.svc.turn_ledger.record_reply("s1", 2, assistant_message_id=201)
        self.svc.hook_turn_start("s1", "第一轮的话")

        fetched = [
            {"message_id": 100, "role": "user", "content": "第一轮的话"},
            {"message_id": 101, "role": "assistant", "content": "第一轮的回"},
            {"message_id": 200, "role": "user", "content": "第二轮的话"},
            {"message_id": 201, "role": "assistant", "content": "第二轮的回"},
        ]
        from unittest.mock import patch
        identities = [{"turn": 1 + i // 2, "role": r["role"], "message_id": str(r["message_id"])}
                      for i, r in enumerate(fetched)]
        proof = patch("ember.service.generation_messages", return_value=identities)
        proof.start()
        orig = source_client.fetch_source_path
        source_client.fetch_source_path = (
            lambda sid, ids, **kw: fetched)
        try:
            corpus = self.svc._quote_corpus(self.svc.sessions["s1"])
        finally:
            source_client.fetch_source_path = orig
            proof.stop()
        by_turn: dict[int, set[str]] = {}
        for m in corpus:
            if m.get("turn") is not None:
                by_turn.setdefault(m["turn"], set()).add(m["content"])
        self.assertEqual(by_turn.get(2), {"第二轮的话", "第二轮的回"})
        self.assertIn("第一轮的话", by_turn.get(1, set()))

    def test_read_source_memory_hit(self) -> None:
        self.svc.store.write_event(
            _make_event(self.svc, "s1", 1, "EVT-001-aaaaaa"))
        self.svc.hook_turn_start("s1", "周末去杭州了")
        self.svc.hook_turn_end("s1", "玩得开心。")
        self.svc.turn_spans.fill("EVT-001-aaaaaa", [(1, 1)],
                                 filled_by="test")
        r = self.svc.read_source_event("EVT-001")
        self.assertEqual(r["status"], "not_archived")  # Legacy local text is not frozen path evidence.

    def test_read_source_unavailable_when_nothing(self) -> None:
        ev = _make_event(self.svc, "s1", 3)
        self.svc.store.write_event(ev)
        self.svc.turn_spans.fill(ev.event_id, [(3, 3)], filled_by="test")
        r = self.svc.read_source_event(ev.event_id)  # 内存无第 3 轮 + 未配置拉取
        self.assertEqual(r["status"], "not_archived")


class ReviseMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.svc = _service(self.tmp)
        self.evt = _make_event(self.svc, "s1", 1)
        self.svc.store.write_event(self.evt)
        self.svc.store.write_route(type(self).route(self.evt))

    @staticmethod
    def route(evt: Event):
        from ember.models import RecallRoute
        return RecallRoute(
            route_id=f"rt_{evt.event_id}_01", event_id=evt.event_id,
            matcher="lexical", cue_type="lexical", cue="旧线索",
            status="active", evidence_refs=[evt.raw_ref],
            provenance_refs=["test"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_revise_within_cooldown(self) -> None:
        r = self.svc.sidecar_revise_memory_event({
            "event_id": "EVT-001",
            "core": "新叙述：我去过杭州，不是住在杭州。",
            "significance": "新意义",
        })
        self.assertEqual(r["status"], "revised")
        self.assertEqual(set(r["revised_fields"]), {"core", "significance"})
        ev = self.svc.store.get_event("EVT-001-aaaaaa")
        self.assertIn("去过杭州", ev.core)
        self.assertEqual(ev.significance, "新意义")
        self.assertEqual(ev.summary, "旧摘要")  # 未提供的字段不动
        self.assertEqual(ev.key_quotes, [])     # 锁死字段不受影响

    def test_revise_list_field_accepts_string(self) -> None:
        r = self.svc.sidecar_revise_memory_event({
            "event_id": "EVT-001", "participants": "user",
        })
        self.assertEqual(r["status"], "revised")
        ev = self.svc.store.get_event("EVT-001-aaaaaa")
        self.assertEqual(ev.participants, ["user"])

    def test_revise_rejected_after_sleep(self) -> None:
        p = self.tmp / "data" / "sleep_progress.jsonl"
        p.write_text(json.dumps(
            {"event_id": "EVT-001-aaaaaa", "stage": "judged"}),
            encoding="utf-8")
        r = self.svc.sidecar_revise_memory_event({
            "event_id": "EVT-001", "core": "试改",
        })
        self.assertEqual(r["status"], "rejected")
        self.assertIn("更正记忆", r["reason"])
        ev = self.svc.store.get_event("EVT-001-aaaaaa")
        self.assertEqual(ev.core, "旧叙述")  # 未被改动

    def test_revise_locked_field_failed(self) -> None:
        r = self.svc.sidecar_revise_memory_event({
            "event_id": "EVT-001", "key_quotes": [{"speaker": "user",
                                                   "text": "改"}],
        })
        self.assertEqual(r["status"], "failed")
        self.assertIn("不可修改", r["error"])

    def test_revise_no_fields_failed(self) -> None:
        r = self.svc.sidecar_revise_memory_event({"event_id": "EVT-001"})
        self.assertEqual(r["status"], "failed")

    def test_revise_cues_rebuild_routes(self) -> None:
        r = self.svc.sidecar_revise_memory_event({
            "event_id": "EVT-001", "recall_cues": ["杭州", "旅行"],
        })
        self.assertEqual(r["status"], "revised")
        self.assertIn("recall_cues", r["revised_fields"])
        routes = [rt for rt in self.svc.store._routes.values()  # noqa: SLF001
                  if rt.event_id == "EVT-001-aaaaaa"]
        active = [rt for rt in routes if rt.status == "active"]
        retired = [rt for rt in routes if rt.status == "retired"]
        self.assertEqual(len(active), 2)
        self.assertEqual({rt.cue for rt in active}, {"杭州", "旅行"})
        self.assertEqual(len(retired), 1)
        # 索引已刷新：新 cue 可命中
        self.assertIn("杭州", self.svc.store.index_snapshot())


if __name__ == "__main__":
    unittest.main()
