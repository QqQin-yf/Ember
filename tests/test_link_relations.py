# -*- coding: utf-8 -*-
"""挂关系三接口（2026-10-14 设计稿 v2：借窗口跑）的验收。

旧版 sleep_v2 脚本（ember 自发 LLM 判联）的落库规则在 service 版逐条
对齐——本文件验的是接口层的完整闭环：
  接口 1 材料出队（batches）→ 接口 2 交卷（submit 落库三分支）
  → 接口 3 进度（progress 断点续跑）→ pending 账本的扣减纪律。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.service import EmberService
from ember.store import EmberStore
from ember.llm import MockLLMClient

NOW = "2026-10-14T00:00:00+08:00"


def _service(tmp: Path) -> EmberService:
    return EmberService(EmberStore(str(tmp / "data")), MockLLMClient(),
                        model="test")


def _add(svc: EmberService, eid: str, turn: int) -> None:
    svc.store.write_event(Event_min(eid, turn))


def Event_min(eid: str, turn: int):
    from ember.models import Event
    return Event(
        event_id=eid, version_id=f"{eid}-v1",
        source_ref=f"conversation/session-s1/turn-{turn}",
        raw_ref=f"conversation/session-s1/turn-{turn}",
        event_time=NOW, ingest_time=NOW,
        summary=f"{eid} 摘要", core=f"{eid} 的完整叙述",
    )


class LinkRelationsTests(unittest.TestCase):
    """挂关系三接口闭环：出队 → 交卷落库 → 进度 → pending 纪律。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _service(Path(self._tmp.name))
        # 旧事件 EVT-OLD（turn 1-2），新事件 EVT-NEW（turn 3）；
        # turn 3 召回带出 EVT-OLD —— 材料 = NEW + 候选 [OLD]
        _add(self.svc, "EVT-OLD", 1)
        _add(self.svc, "EVT-NEW", 3)
        self.svc.turn_spans.fill("EVT-OLD", [(1, 2)], filled_by="test")
        self.svc.turn_spans.fill("EVT-NEW", [(3, 3)], filled_by="test")
        self.svc.recall_ledger.record("s1", 3, "cue", ["EVT-OLD"], cue="c")
        self.svc.link_pending_add("EVT-NEW")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _batches(self) -> dict:
        return self.svc.sidecar_link_batches()

    def test_batches_material_shape(self) -> None:
        """接口 1：材料含新事件 + 候选旧事件（带实体位），pending 不扣。"""
        out = self._batches()
        self.assertEqual(len(out["batches"]), 1)
        m = out["batches"][0]["materials"][0]
        self.assertEqual(m["new_id"], "EVT-NEW")
        self.assertEqual(m["new_event"]["core"], "EVT-NEW 的完整叙述")
        self.assertEqual([o["event_id"] for o in m["old_events"]],
                         ["EVT-OLD"])
        # 出队幂等：再调一次材料照旧（扣减只发生在交卷）
        out2 = self._batches()
        self.assertEqual(out2["materials_total"], 1)
        self.assertEqual(out2["pending_total"], 1)

    def test_submit_relation(self) -> None:
        """接口 2：relation 落边 + pair 留痕 + pending 扣减。"""
        material = self._batches()["batches"][0]["materials"][0]
        verdict = {"pairs": [{"old_event_id": "EVT-OLD",
                              "outcome": "relation", "type": "related_to",
                              "confidence": "high", "reason": "同一件事"}],
                   "entity": {"action": "none"}}
        out = self.svc.sidecar_link_submit(
            {"results": [{"material": material, "verdict": verdict}]})
        self.assertEqual(len(out["saved"]), 1)
        self.assertEqual(out["rejected"], [])
        self.assertEqual(out["remaining_pending"], [])
        rels = self.svc.relation_store.all()
        self.assertEqual(len(rels), 1)
        self.assertEqual(rels[0].src_event, "EVT-OLD")   # 方向固定：旧 → 新
        self.assertEqual(rels[0].dst_event, "EVT-NEW")
        self.assertEqual(rels[0].type, "related_to")
        self.assertTrue(self.svc.pair_seen.is_seen("EVT-NEW", "EVT-OLD"))
        # 旧事件未被推翻：状态仍 valid、无 change
        self.assertTrue(self.svc.sleep_state.recall_allowed("EVT-OLD"))

    def test_submit_resolves_marks_superseded(self) -> None:
        """resolves 三处落盘：边 + superseded 标记 + change 补写。"""
        material = self._batches()["batches"][0]["materials"][0]
        verdict = {"pairs": [{"old_event_id": "EVT-OLD",
                              "outcome": "relation", "type": "resolves",
                              "confidence": "high", "reason": "口径变了",
                              "change": "后来知道其实不是那样",
                              "status_evidence": "用户亲口纠正"}],
                   "entity": {"action": "none"}}
        out = self.svc.sidecar_link_submit(
            {"results": [{"material": material, "verdict": verdict}]})
        self.assertEqual(len(out["saved"]), 1)
        self.assertEqual(out["saved"][0]["resolves"], ["EVT-OLD"])
        self.assertEqual(self.svc.sleep_state.status_of("EVT-OLD"),
                         "superseded")
        self.assertFalse(self.svc.sleep_state.recall_allowed("EVT-OLD"))
        self.assertEqual(self.svc.store.get_event("EVT-OLD").change,
                         "后来知道其实不是那样")

    def test_submit_rejects_outside_material(self) -> None:
        """防幻觉：pair 指向材料外事件 → 整份拒绝，pending 不扣。"""
        material = self._batches()["batches"][0]["materials"][0]
        verdict = {"pairs": [{"old_event_id": "EVT-GHOST",
                              "outcome": "relation", "type": "related_to",
                              "confidence": "high", "reason": "编的"}],
                   "entity": {"action": "none"}}
        out = self.svc.sidecar_link_submit(
            {"results": [{"material": material, "verdict": verdict}]})
        self.assertEqual(out["saved"], [])
        self.assertEqual(len(out["rejected"]), 1)
        self.assertEqual(out["remaining_pending"], ["EVT-NEW"])

    def test_progress_reports_pending(self) -> None:
        """接口 3：进度 = pending 剩余 + 可组材料 + 剩余批数。"""
        out = self.svc.sidecar_link_progress()
        self.assertEqual(out["pending_total"], 1)
        self.assertEqual(out["materials_ready"], 1)
        self.assertEqual(out["batches_remaining"], 1)
        self.assertIn("EVT-NEW", out["pending_events"])

    def test_batches_capped_at_five(self) -> None:
        """6 份材料 → 5 批封顶（软约束落地）。"""
        for i in range(6):
            eid = f"EVT-N{i}"
            _add(self.svc, eid, 10 + i)
            self.svc.turn_spans.fill(eid, [(10 + i, 10 + i)],
                                     filled_by="test")
            self.svc.recall_ledger.record("s1", 3, "cue", ["EVT-OLD"],
                                          cue=f"c{i}")
            self.svc.link_pending_add(eid)
        out = self._batches()
        # 约束：≤5 批清完（越少越好），材料一份不丢
        self.assertLessEqual(len(out["batches"]), 5)
        total = sum(len(b["materials"]) for b in out["batches"])
        self.assertEqual(total, out["materials_total"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
