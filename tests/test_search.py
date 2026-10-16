# -*- coding: utf-8 -*-
"""search_memory 主动检索（2026-10-15 设计夜批 1）的验收。

覆盖：k 钳制 / time_range 解析 / 语义主模态（mock 向量）+ LIKE 兜底 /
状态标注（faded/superseded+修正者指针）/ 实体卡 / 形态判定 / 检索即
提取（记账+计时器刷新+retire 可逆）/ 夜批补向量。
向量服务 mock：patch ember.service.embed_text。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.service import EmberService
from ember.store import EmberStore
from ember.llm import MockLLMClient

NOW = "2026-10-15T12:00:00+08:00"
VEC_A = [1.0, 0.0, 0.0]
VEC_B = [0.0, 1.0, 0.0]
VEC_Q = [0.9, 0.1, 0.0]      # 与 A 高相似
VEC_FAR = [0.0, 0.0, 1.0]    # 与谁都不像


def _service(tmp: Path) -> EmberService:
    return EmberService(EmberStore(str(tmp / "data")), MockLLMClient(),
                        model="test")


def _add(svc: EmberService, eid: str, core: str,
         ingest: str = NOW) -> None:
    from ember.models import Event
    svc.store.write_event(Event(
        event_id=eid, version_id=f"{eid}-v1",
        source_ref=f"conversation/session-s1/turn-1",
        raw_ref=f"conversation/session-s1/turn-1",
        event_time=ingest, ingest_time=ingest,
        summary=f"{eid} 摘要", core=core,
    ))


def _mk_entity(svc: EmberService, eid: str, name: str, summary: str = ""):
    from ember.entity import Entity
    svc.entity_store.upsert(Entity(entity_id=eid, name=name,
                                   summary=summary))
    svc.entity_store.save()


class SearchMemoryTests(unittest.TestCase):
    """search 闭环：向量命中 → 标注 → 形态 → 记账 → restore。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _service(Path(self._tmp.name))
        _add(self.svc, "EVT-001", "做了子夜阁的五集连续漫剧")
        _add(self.svc, "EVT-002", "讨论了漫剧的分镜设计")
        _add(self.svc, "EVT-003", "完全无关的事件内容")

        def fake_embed(text: str):
            if "漫画" in text or "漫剧" in text:
                return VEC_A
            return VEC_FAR

        self._p = mock.patch("ember.service.embed_text",
                             side_effect=fake_embed)
        self._p.start()
        # 预填向量库（模拟离线重建）
        for eid, vec in (("EVT-001", VEC_A), ("EVT-002", VEC_A),
                         ("EVT-003", VEC_FAR)):
            ev = self.svc.store.get_event(eid)
            self.svc.core_vectors.upsert(eid, ev.core or ev.summary,
                                         vec, "test-model")

    def tearDown(self) -> None:
        self._p.stop()
        self._tmp.cleanup()

    def test_semantic_hits_and_form(self) -> None:
        """语义命中排序；两条同实体命中判 chain（未归户时 cluster）。"""
        out = self.svc.sidecar_search_memory({"query": "漫剧"})
        self.assertEqual(out["status"], "ok")
        ids = [it["event_id"] for it in out["items"]]
        self.assertIn("EVT-001", ids)
        self.assertNotIn("EVT-003", ids)   # 不像的不出
        self.assertEqual(out["relation"], "cluster")
        self.assertEqual(out["total"], 2)

    def test_k_clamped(self) -> None:
        out = self.svc.sidecar_search_memory({"query": "漫剧", "k": 99})
        self.assertEqual(len(out["items"]), 2)   # 库里只有 2 条相关，5 上限不触
        self.assertIn("上限 5", out["note"])
        out2 = self.svc.sidecar_search_memory({"query": "漫剧", "k": 1})
        self.assertEqual(len(out2["items"]), 1)
        self.assertIn("仅显示", out2["note"])

    def test_time_range_filter(self) -> None:
        out = self.svc.sidecar_search_memory(
            {"query": "漫剧", "time_range": "2026-10-01..2026-10-14"})
        self.assertEqual(out["items"], [])   # 事件 ingest 是 10-15，区间不含
        out2 = self.svc.sidecar_search_memory(
            {"query": "漫剧", "time_range": "最近3天"})
        self.assertEqual(out2["total"], 2)
        out3 = self.svc.sidecar_search_memory(
            {"query": "漫剧", "time_range": "胡说八道"})
        self.assertEqual(out3["status"], "failed")

    def test_query_required(self) -> None:
        out = self.svc.sidecar_search_memory({})
        self.assertEqual(out["status"], "failed")

    def test_like_fallback_with_multi_keywords(self) -> None:
        """embed 挂了 → LIKE 兜底：拆词并集，多词命中排前，note 告知。"""
        self._p.stop()   # 让 embed_text 真抛（未配 key → RuntimeError）
        out = self.svc.sidecar_search_memory({"query": "漫剧 无关词"})
        self.assertEqual(out["status"], "ok")
        self.assertIn("降级", out["note"])
        self.assertEqual(out["relation"], "cluster")
        ids = [it["event_id"] for it in out["items"]]
        self.assertIn("EVT-001", ids)      #「漫剧」命中
        self.assertNotIn("EVT-003", ids)   #「无关词」不沾
        self._p.start()   # 恢复给 tearDown

    def test_search_records_extraction(self) -> None:
        """检索即提取：命中入账（route=search），last_extracted 刷新。"""
        self.svc.sidecar_search_memory({"query": "漫剧"})
        at = self.svc.recall_ledger.last_extracted.get("EVT-001", "")
        self.assertTrue(at >= NOW, f"最后提取时间应不早于事件写入: {at}")

    def test_faded_flag_and_restore(self) -> None:
        """收紧+30 天未提取 = faded 标注；search 命中即时 restore。"""
        from ember.models import RecallRoute
        # 手动把 EVT-001 的 cue 全部收紧，并把事件做旧（ingest 拉远）
        old = "2026-07-01T00:00:00+08:00"
        ev = self.svc.store.get_event("EVT-001")
        ev.ingest_time = old
        ev.event_time = old
        self.svc.store._events["EVT-001"] = ev   # noqa: SLF001 直接改内存
        self.svc.store.write_route(RecallRoute(
            route_id="rt-001", event_id="EVT-001", cue="漫画",
            matcher="lexical", cue_type="conceptual"))
        for r in self.svc.store.routes_for_event("EVT-001"):
            self.svc.store.retire_route(r.route_id)
        self.assertTrue(self.svc.is_faded("EVT-001"))

        out = self.svc.sidecar_search_memory({"query": "漫剧"})
        item = next(it for it in out["items"]
                    if it["event_id"] == "EVT-001")
        self.assertEqual(item["status"], "faded")
        # 即时 restore：cue 活了，faded 自动解除
        self.assertTrue(all(r.status == "active" for r in
                            self.svc.store.routes_for_event("EVT-001")))
        self.assertFalse(self.svc.is_faded("EVT-001"))

    def test_superseded_pointer(self) -> None:
        """superseded 命中带修正者指针。"""
        self.svc.sleep_state.mark("EVT-001", "superseded", "被 EVT-002 修正",
                                  "test")
        self.svc.relation_store.add(_rel("EVT-001", "EVT-002"))
        out = self.svc.sidecar_search_memory({"query": "漫剧"})
        item = next(it for it in out["items"]
                    if it["event_id"] == "EVT-001")
        self.assertEqual(item["status"], "superseded")
        self.assertEqual(item["superseded_by"], ["EVT-002"])

    def test_entity_card(self) -> None:
        """实体 summary 向量命中 → 实体卡（印象层先出）。"""
        _mk_entity(self.svc, "ent_ziye", "子夜阁项目",
                   "子夜阁五集连续漫剧的制作")
        self.svc.entity_vectors.upsert("ent_ziye", "子夜阁五集连续漫剧的制作",
                                       VEC_A, "test-model")
        out = self.svc.sidecar_search_memory({"query": "漫剧"})
        self.assertEqual(len(out["entity_cards"]), 1)
        card = out["entity_cards"][0]
        self.assertEqual(card["name"], "子夜阁项目")
        self.assertEqual(card["member_count"], 0)   # 事件未归户

    def test_night_backfill(self) -> None:
        """夜批补向量：无向量的新事件被补上，已有的不重复。"""
        _add(self.svc, "EVT-009", "夜里新写的记忆内容")
        with mock.patch("ember.executors.call_llm",
                        return_value='{"assignments": []}'):
            report = self.svc.run_structure_maintenance()
        self.assertEqual(report["vector_backfill"]["events"], 1)
        self.assertTrue(self.svc.core_vectors.has("EVT-009"))
        with mock.patch("ember.executors.call_llm",
                        return_value='{"assignments": []}'):
            report2 = self.svc.run_structure_maintenance()
        self.assertEqual(report2["vector_backfill"]["events"], 0)


def _rel(src: str, dst: str):
    from ember.relation import EventRelation
    return EventRelation(relation_id="rel_test0001", src_event=src,
                         dst_event=dst, type="resolves",
                         confidence="high",
                         evidence_refs=["test"], provenance_refs=["test"])


def _mk_entity(svc: EmberService, eid: str, name: str, summary: str = ""):
    from ember.entity import Entity
    svc.entity_store.upsert(Entity(entity_id=eid, name=name,
                                   summary=summary))
    svc.entity_store.save()


if __name__ == "__main__":
    unittest.main()
