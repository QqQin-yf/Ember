# -*- coding: utf-8 -*-
"""批 2（2026-10-15 设计夜定稿）：⑦召回门槛 + ⑧在场去重 的验收。

覆盖：
- 路按源数不按锚数：多 cue 同源合并 = 1 路（单源落弱信号池）
- 主席三资格：双源共证 / 修正者顶上（relation 路不受限）/
  新生豁免（48h 内限 2 席，超额挪弱信号池头部优先回补）
- 弱信号池：席位富余才补（weak=True），不足落选（weak_pooled）
- 实体块：块级资格（成员并集源数 ≥2），整块 1 席
- stats 三计数：dropped_by_seats / weak_pooled / strong_dropped
- 在场去重（hooks 全链路）：首注全量 → 2h 内轻量续注 → 超时重新全量
- 前缀渲染：轻量行 / 弱标注 / 尾部检索提示（构造 ctx 直测）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.llm import MockLLMClient
from ember.models import (Event, PresentationContext, RecallTrace, RouteHit)
from tests.test_sidecar import write_args
from ember.recall import EntityHit, EntityRecall
from tests.legacy_event_fixture import select_recall_units
from tests.legacy_event_fixture import LegacyEventService as EmberService
from ember.store import EmberStore
from ember.direction import DirectionMatcher, DirectionVectorStore
from ember.entity import Direction, Entity, EntityStore

NOW = "2026-10-15T12:00:00+08:00"
FRESH = "2026-10-15T11:00:00+08:00"     # 1h 前——48h 豁免窗口内
RECENT = "2026-10-13T00:00:00+08:00"    # 旧但较新——豁免窗口（48h）外
OLD = "2026-07-01T00:00:00+08:00"       # 3 个多月前——新生豁免之外


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _add(svc: EmberService, eid: str, core: str, ingest: str = OLD) -> Event:
    return svc.store.write_event(Event(
        event_id=eid, version_id=f"{eid}-v1",
        source_ref="conversation/session-s1/turn-1",
        raw_ref="conversation/session-s1/turn-1",
        event_time=ingest, ingest_time=ingest,
        summary=f"{eid} 摘要", core=core,
    ))


def _own(svc: EmberService, eid: str, ent: str) -> None:
    """事件挂到实体（单归属铁律取第一个）。"""
    ev = svc.store.get_event(eid)
    ev.entity_refs = [ent]
    svc.store._events[eid] = ev   # noqa: SLF001 与 test_search 同口径


def _hit(eid: str, cue: str, matcher: str = "lexical") -> RouteHit:
    return RouteHit(route_id=f"rt-{eid}-{cue}", event_id=eid, cue=cue,
                    matcher=matcher, cue_type="conceptual")


def _ehit(ent: str, name: str) -> EntityHit:
    return EntityHit(entity_id=ent, name=name, matched_via=name)


def _ids(selected: list[dict]) -> list[str]:
    return [e for u in selected for e in u["event_ids"]]


class GateTests(unittest.TestCase):
    """⑦ 召回门槛：多源共证 / relation 路不受限 / 新生限额 / 弱信号池 / 实体块。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = EmberService(EmberStore(str(Path(self._tmp.name) / "data")),
                                MockLLMClient(), model="test")
        self.now = _dt(NOW)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _sel(self, recalled, anchors, entity_hits=(), resolved=(), seats=3):
        return select_recall_units(recalled, list(anchors), list(entity_hits),
                                   list(resolved), self.svc.store,
                                   seats=seats, now=self.now)

    def test_route_count_counts_sources_not_anchors(self):
        """多 cue 同源 = 1 路：两个 lexical 命中仍是单源，只能弱补位。"""
        _add(self.svc, "EVT-A", "爱好是画漫画，坚持了很多年")
        anchors = [_hit("EVT-A", "爱好"), _hit("EVT-A", "漫画")]
        selected, stats = self._sel(["EVT-A"], anchors, seats=1)
        self.assertEqual(len(selected), 1)
        u = selected[0]
        self.assertEqual(u["route_count"], 1)   # 按源数不按锚数
        self.assertEqual(u["cue_count"], 2)     # 但锚数被记录
        self.assertTrue(u.get("weak"))          # 单源 = 弱信号补位
        self.assertEqual(stats["strong_dropped"], 0)

    def test_multi_cue_weak_pools_priority(self):
        """弱池补位锚数优先：多 cue 命中的旧事压过较新的单 cue 命中。"""
        _add(self.svc, "EVT-A", "爱好是画漫画，一直坚持", ingest=OLD)
        _add(self.svc, "EVT-B", "较近聊到的单线索话题", ingest=RECENT)
        anchors = ([_hit("EVT-A", "爱好"), _hit("EVT-A", "漫画")]
                   + [_hit("EVT-B", "话题")])
        selected, stats = self._sel(["EVT-B", "EVT-A"], anchors, seats=1)
        # 两条都是单源弱信号（都不在新生窗口内）：A 锚数 2 > B 的 1
        self.assertEqual(_ids(selected), ["EVT-A"])
        self.assertTrue(selected[0].get("weak"))
        self.assertEqual(stats["weak_pooled"], 1)   # B 落池

    def test_same_cue_dual_matcher_counts_once(self):
        """同一 cue 文本的 lexical+semantic 双中算 1 个锚。"""
        _add(self.svc, "EVT-A", "爱好是画漫画，坚持了很久", ingest=OLD)
        _add(self.svc, "EVT-B", "较近的单线索事件", ingest=RECENT)
        anchors = ([_hit("EVT-A", "爱好", "lexical"),
                    _hit("EVT-A", "爱好", "semantic")]
                   + [_hit("EVT-B", "另一词")])
        selected, _ = self._sel(["EVT-A", "EVT-B"], anchors, seats=1)
        # A 锚数去重后 = 1，与 B 同级 → 退回按新旧排，B 较新 → B 先补位
        self.assertEqual(_ids(selected), ["EVT-B"])

    def test_multisource_qualified_and_weak_fill(self):
        """cue+entity 双源进主席；单源席位富余才补位（weak=True）。"""
        _add(self.svc, "EVT-A", "爱好是画漫画，坚持了很多年")
        _add(self.svc, "EVT-B", "讨论了漫画的分镜设计")
        _own(self.svc, "EVT-B", "ent_x")
        anchors = [_hit("EVT-A", "爱好"), _hit("EVT-B", "漫画")]
        ehits = [_ehit("ent_x", "漫画项目")]
        selected, stats = self._sel(["EVT-A", "EVT-B"], anchors, ehits, seats=1)
        self.assertEqual(_ids(selected), ["EVT-B"])   # 双源独占
        self.assertEqual(stats["weak_pooled"], 1)
        selected2, stats2 = self._sel(["EVT-A", "EVT-B"], anchors, ehits,
                                      seats=2)
        self.assertEqual(_ids(selected2), ["EVT-B", "EVT-A"])
        weak_u = next(u for u in selected2 if u["event_ids"] == ["EVT-A"])
        self.assertTrue(weak_u.get("weak"))
        self.assertEqual(stats2["weak_pooled"], 0)

    def test_relation_route_unrestricted(self):
        """修正者顶上不受门槛限制：单源继承命中也进主席。"""
        _add(self.svc, "EVT-S", "我曾以为爱好是画漫画")
        _add(self.svc, "EVT-D", "后来发现真正喜欢的是摄影")
        anchors = [_hit("EVT-S", "爱好")]
        resolved = [("EVT-D", "EVT-S")]   # (修正者, 被修正者)
        selected, stats = self._sel(["EVT-S", "EVT-D"], anchors, (), resolved,
                                    seats=1)
        self.assertEqual(_ids(selected), ["EVT-D"])
        self.assertTrue(selected[0].get("resolved"))

    def test_fresh_exemption_two_seats_overflow_pooled(self):
        """新生豁免 48h 内限 2 席：超额挪弱信号池头部，席位富余优先回补。"""
        _add(self.svc, "OLD1", "最早的讨论一，双源共证")
        _own(self.svc, "OLD1", "ent_1")
        _add(self.svc, "OLD2", "最早的讨论二，双源共证")
        _own(self.svc, "OLD2", "ent_2")
        for i in (1, 2, 3):
            _add(self.svc, f"F{i}", f"刚发生的新事 {i}", ingest=FRESH)
        anchors = ([_hit("OLD1", "讨论一"), _hit("OLD2", "讨论二")]
                   + [_hit(f"F{i}", f"新事{i}") for i in (1, 2, 3)])
        ehits = [_ehit("ent_1", "老项目一"), _ehit("ent_2", "老项目二")]
        recalled = ["F1", "F2", "F3", "OLD1", "OLD2"]

        # 席位被两条共证老事占满：新生只保 1 席，F2/F3 被挤出
        # （新生单源不计 strong_dropped——尾部提示只给共证落选）
        selected, stats = self._sel(recalled, anchors, ehits, seats=3)
        self.assertEqual(_ids(selected), ["OLD1", "OLD2", "F1"])
        self.assertEqual(stats["weak_pooled"], 0)
        self.assertEqual(stats["strong_dropped"], 0)
        self.assertEqual(stats["dropped_by_seats"], 2)

        # 席位富余 1：F2 补入，F3 仍被挤出
        selected2, stats2 = self._sel(recalled, anchors, ehits, seats=4)
        self.assertEqual(_ids(selected2), ["OLD1", "OLD2", "F1", "F2"])
        self.assertEqual(stats2["dropped_by_seats"], 1)

    def test_fresh_overflow_moves_to_pool_head_and_backfills(self):
        """超额新生先进选、再降级：腾出的席位由池头立即回补（标 weak）。"""
        for i in (1, 2, 3):
            _add(self.svc, f"F{i}", f"刚发生的新事 {i}", ingest=FRESH)
        anchors = [_hit(f"F{i}", f"新事{i}") for i in (1, 2, 3)]
        recalled = ["F1", "F2", "F3"]

        # seats=3：三条新生全进选 → 限额降级 F3 → 腾出的席位由池头
        # 立即回补（weak=True，渲染降级为「单线索」行）
        selected, stats = self._sel(recalled, anchors, (), seats=3)
        self.assertEqual(_ids(selected), ["F1", "F2", "F3"])
        f3 = next(u for u in selected if u["event_ids"] == ["F3"])
        self.assertTrue(f3.get("weak"))
        self.assertEqual(stats["weak_pooled"], 0)
        self.assertEqual(stats["dropped_by_seats"], 0)

        # seats=2：F3 从未进选，直接被名额挤出（无池可落）
        selected2, stats2 = self._sel(recalled, anchors, (), seats=2)
        self.assertEqual(_ids(selected2), ["F1", "F2"])
        self.assertEqual(stats2["dropped_by_seats"], 1)
        self.assertEqual(stats2["weak_pooled"], 0)

    def test_entity_block_block_level_qualification_one_seat(self):
        """实体块：块级资格（成员并集源数 ≥2）+ 整块 1 席。"""
        _add(self.svc, "EVT-A", "项目立项讨论")
        _add(self.svc, "EVT-B", "项目第一版上线")
        _add(self.svc, "EVT-C", "项目复盘会议")
        for e in ("EVT-A", "EVT-B", "EVT-C"):
            _own(self.svc, e, "ent_x")
        anchors = [_hit("EVT-A", "项目")]             # 只有一个成员有 cue
        ehits = [_ehit("ent_x", "大项目")]
        selected, stats = self._sel(["EVT-A", "EVT-B", "EVT-C"], anchors,
                                    ehits, seats=1)
        self.assertEqual(len(selected), 1)
        blk = selected[0]
        self.assertEqual(blk["kind"], "entity_block")
        self.assertEqual(blk["event_ids"], ["EVT-A", "EVT-B", "EVT-C"])
        self.assertEqual(blk["route_count"], 2)       # cue + entity
        self.assertEqual(stats["dropped_by_seats"], 0)

    def test_entity_block_pure_entity_single_source_is_weak(self):
        """块内只有实体一路（无成员 cue）= 单源 → 只能弱补位/落池。"""
        _add(self.svc, "EVT-B", "项目第一版上线")
        _own(self.svc, "EVT-B", "ent_x")
        ehits = [_ehit("ent_x", "大项目")]
        # 席位富余：补位且标 weak
        selected, stats = self._sel(["EVT-B"], [], ehits, seats=1)
        self.assertEqual(_ids(selected), ["EVT-B"])
        self.assertTrue(selected[0].get("weak"))
        self.assertEqual(stats["weak_pooled"], 0)
        # 席位被共证事件占满：整块落池
        _add(self.svc, "EVT-S", "无关但双源共证的事")
        _own(self.svc, "EVT-S", "ent_y")
        anchors2 = [_hit("EVT-S", "无关")]
        ehits2 = ehits + [_ehit("ent_y", "另一项目")]
        selected2, stats2 = self._sel(["EVT-B", "EVT-S"], anchors2, ehits2,
                                      seats=1)
        self.assertEqual(_ids(selected2), ["EVT-S"])  # 双源独占席位
        self.assertEqual(stats2["weak_pooled"], 1)    # 纯实体块落池

    def test_pure_entity_block_sinks_below_cue_hits(self):
        """弱池内锚数排序：纯实体块（0 cue）沉到任何 cue 命中之下。"""
        _add(self.svc, "EVT-B", "项目第一版上线")
        _own(self.svc, "EVT-B", "ent_x")
        _add(self.svc, "EVT-C", "较近的单线索事件", ingest=RECENT)
        anchors = [_hit("EVT-C", "事件")]
        ehits = [_ehit("ent_x", "大项目")]
        selected, stats = self._sel(["EVT-B", "EVT-C"], anchors, ehits,
                                    seats=1)
        # 两者皆单源弱信号：C 有 1 个 cue，块 0 个 → C 补位，块落池
        self.assertEqual(_ids(selected), ["EVT-C"])
        self.assertEqual(stats["weak_pooled"], 1)

    def test_strong_dropped_drives_hint(self):
        """共证却被名额挤出 → strong_dropped 计数（驱动尾部检索提示）。"""
        for i in (1, 2, 3):
            _add(self.svc, f"M{i}", f"多源事件 {i}")
            _own(self.svc, f"M{i}", f"ent_{i}")   # 各自实体，互不成块
        anchors = [_hit(f"M{i}", f"事件{i}") for i in (1, 2, 3)]
        ehits = [_ehit(f"ent_{i}", f"项目{i}") for i in (1, 2, 3)]
        selected, stats = self._sel(["M1", "M2", "M3"], anchors, ehits,
                                    seats=2)
        self.assertEqual(_ids(selected), ["M1", "M2"])
        self.assertEqual(stats["strong_dropped"], 1)
        self.assertEqual(stats["dropped_by_seats"], 1)
        self.assertEqual(stats["weak_pooled"], 0)


class InjectionDedupTests(unittest.TestCase):
    """⑧ 在场去重：首注全量 → 2h 内轻量续注 → 超时重新全量（hooks 全链路）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.client = MockLLMClient()
        self.svc = EmberService(EmberStore(str(Path(self._tmp.name) / "data")),
                                self.client, model="test")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_full_then_light_then_full_again(self) -> None:
        sid = "s_dedup"
        self.svc.hook_turn_start(sid, "今天我们决定把 Ember 的传播模型改成 Route Search。",user_message_id=71)
        written = self.svc.sidecar_call_tool("write_event", write_args())
        self.assertEqual(written["status"], "created")
        eid = written["event_id"]
        full_mark = f'read_memory_source("{eid}")'
        self.svc.hook_turn_end(sid, "已确认。")

        full = self.svc.hook_turn_start(sid, "我们继续聊 Ember 吧。")["injected_text"]
        self.assertIn("[记忆浮现]", full)
        self.assertIn("触点：", full)
        self.assertIn(full_mark, full)
        self.assertNotIn("（此前已浮现）", full)
        self.svc.hook_turn_end(sid, "继续。")

        light = self.svc.hook_turn_start(sid, "再聊聊 Ember。")["injected_text"]
        self.assertIn("（此前已浮现）", light)
        self.assertNotIn(full_mark, light)
        self.svc.hook_turn_end(sid, "继续。")

        stale = (datetime.now(timezone.utc).astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
        self.svc.recall_ledger.last_injected[(sid, eid)] = stale
        again = self.svc.hook_turn_start(sid, "还想聊 Ember。")["injected_text"]
        self.assertIn(full_mark, again)
        self.assertEqual(self.client.calls, [])


class PrefixRenderTests(unittest.TestCase):
    """多源共证呈现：轻量行 / 弱标注 / 尾部检索提示（直测渲染）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = EmberService(EmberStore(str(Path(self._tmp.name) / "data")),
                                MockLLMClient(), model="test")
        _add(self.svc, "EVT-A", "爱好是画漫画")
        _add(self.svc, "EVT-B", "漫画分镜讨论")
        self.ctx = PresentationContext(
            recalled_events=["EVT-A"],
            recall_trace=RecallTrace(input_text="聊爱好",
                                     anchors=[_hit("EVT-A", "爱好")]))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_light_line(self) -> None:
        p = self.svc._build_recall_prefix(
            self.ctx, units=[{"kind": "event", "event_ids": ["EVT-A"],
                              "light": True}])
        self.assertIn("（此前已浮现）", p)
        self.assertNotIn("触点：", p)

    def test_light_block_line(self) -> None:
        p = self.svc._build_recall_prefix(
            self.ctx, entity_hits=[_ehit("ent_x", "漫画项目")],
            units=[{"kind": "entity_block", "entity_id": "ent_x",
                    "event_ids": ["EVT-A", "EVT-B"], "light": True}])
        self.assertIn("〈漫画项目〉名下的 2 条经历", p)

    def test_weak_label(self) -> None:
        p = self.svc._build_recall_prefix(
            self.ctx, units=[{"kind": "event", "event_ids": ["EVT-A"],
                              "weak": True, "cue_count": 1}])
        self.assertIn("（单线索浮现，可能只是沾边）", p)
        self.assertIn("爱好是画漫画", p)   # 事件行照常给出

    def test_multi_cue_weak_no_single_clue_label(self) -> None:
        """多 cue 弱补位：不标「单线索」，强度交给事件行的多线索标注。"""
        ctx2 = PresentationContext(
            recalled_events=["EVT-A"],
            recall_trace=RecallTrace(input_text="聊爱好漫画",
                                     anchors=[_hit("EVT-A", "爱好"),
                                              _hit("EVT-A", "漫画")]))
        p = self.svc._build_recall_prefix(
            ctx2, units=[{"kind": "event", "event_ids": ["EVT-A"],
                          "weak": True, "cue_count": 2}])
        self.assertNotIn("单线索", p)
        self.assertIn("多个线索同时指向", p)

    def test_light_line_entity_member_uses_entity_name(self) -> None:
        """成员事件的轻量行：entity summary 打头，member summary 退役。"""
        estore = self.svc.entity_store
        estore.upsert(Entity(entity_id="ent_x", name="漫画项目",
                             summary="实体描述一句话。"))
        estore.save()
        _own(self.svc, "EVT-A", "ent_x")
        p = self.svc._build_recall_prefix(
            self.ctx, units=[{"kind": "event", "event_ids": ["EVT-A"],
                              "light": True}])
        self.assertIn("〈漫画项目〉名下的相关经历（EVT-A）", p)
        self.assertNotIn("EVT-A 摘要", p)

    def test_fallback_members_collapse_to_id_list(self) -> None:
        """summary_fallback：成员收敛为 id 清单，entity summary 打头。"""
        estore = self.svc.entity_store
        estore.upsert(Entity(entity_id="ent_x", name="漫画项目",
                             summary="实体描述一句话。"))
        estore.save()
        _own(self.svc, "EVT-A", "ent_x")
        _own(self.svc, "EVT-B", "ent_x")
        ctx = PresentationContext(
            recalled_events=["EVT-A", "EVT-B"],
            recall_trace=RecallTrace(input_text="x", anchors=[]))
        p = self.svc._build_recall_prefix(
            ctx, entity_hits=[_ehit("ent_x", "漫画项目")],
            summary_fallback=True)
        self.assertIn("关于〈漫画项目〉：实体描述一句话。", p)
        self.assertIn("（名下经历：EVT-A、EVT-B）", p)
        self.assertNotIn("EVT-A 摘要", p)

    def test_strong_dropped_hint(self) -> None:
        p = self.svc._build_recall_prefix(
            self.ctx, units=[{"kind": "event", "event_ids": ["EVT-A"]}],
            strong_dropped=2)
        self.assertIn("另有 2 条高度相关的记忆", p)
        self.assertIn("search_memory", p)
        p0 = self.svc._build_recall_prefix(
            self.ctx, units=[{"kind": "event", "event_ids": ["EVT-A"]}],
            strong_dropped=0)
        self.assertNotIn("另有", p0)


class DirectionFocusGuardTests(unittest.TestCase):
    """聚焦护栏：方向判定「清晰」但该方向名下零挂载 → 退回全区域。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "data"
        self.root.mkdir(parents=True)
        self.store = EmberStore(str(self.root))
        # 3 条事件挂 ent_x，全部没有 direction_refs（挂载从未跑过）
        for i in (1, 2, 3):
            self.store.write_event(Event(
                event_id=f"EVT-{i}", version_id=f"EVT-{i}-v1",
                source_ref="conversation/session-s1",
                raw_ref=f"conversation/session-s1/turn-{i}",
                event_time=OLD, ingest_time=OLD,
                summary=f"事件{i}", core=f"事件{i}的叙述"))
            ev = self.store.get_event(f"EVT-{i}")
            ev.entity_refs = ["ent_x"]
        # 实体带两个方向 + 方向向量（离线预计算产物的最小替身）
        estore = EntityStore(str(self.root))
        estore.upsert(Entity(entity_id="ent_x", name="项目X", directions=[
            Direction(direction_id="dir_a", description="架构与机制设计"),
            Direction(direction_id="dir_b", description="前端界面交互"),
        ]))
        estore.save()
        with open(self.root / "direction_vectors.jsonl", "w",
                  encoding="utf-8") as f:
            f.write(json.dumps({"direction_id": "dir_a",
                                "vector": [1.0, 0.0]}) + "\n")
            f.write(json.dumps({"direction_id": "dir_b",
                                "vector": [0.0, 1.0]}) + "\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_focused_without_mounts_returns_full_region(self) -> None:
        matcher = DirectionMatcher(
            EntityStore(str(self.root)),
            DirectionVectorStore(str(self.root)),
            embed_fn=lambda text: [1.0, 0.0])   # 清晰指向 dir_a
        er = EntityRecall(self.store, EntityStore(str(self.root)),
                          direction_matcher=matcher)
        hits, eids = er.recall("项目X 的架构机制怎么设计的")
        self.assertEqual(len(hits), 1)
        dr = er.last_direction_result
        self.assertTrue(dr.focused and dr.top1[0] == "dir_a",
                        str(dr and dr.to_dict()))
        self.assertEqual(len(eids), 3)   # 零挂载 → 全区域，绝不缩成零


if __name__ == "__main__":
    unittest.main()
