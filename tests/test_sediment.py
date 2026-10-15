# -*- coding: utf-8 -*-
"""沉淀证据接收（2026-10-14 设计稿 v2 第 4 步，10.15 分工对齐版）的验收。

分工：沉淀正文与版本号归 app，ember 只收"某版 + 证据 id"追加——
app 真的存下一版新沉淀时才调；证据为空 app 根本不调。
本文件验：落账、按版本挂证据、去重纪律、防幻觉、同版本覆盖、重载。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.service import EmberService
from ember.store import EmberStore
from ember.llm import MockLLMClient

NOW = "2026-10-15T00:00:00+08:00"


def _service(tmp: Path) -> EmberService:
    return EmberService(EmberStore(str(tmp / "data")), MockLLMClient(),
                        model="test")


def _add(svc: EmberService, eid: str, turn: int) -> None:
    from ember.models import Event
    svc.store.write_event(Event(
        event_id=eid, version_id=f"{eid}-v1",
        source_ref=f"conversation/session-s1/turn-{turn}",
        raw_ref=f"conversation/session-s1/turn-{turn}",
        event_time=NOW, ingest_time=NOW,
        summary=f"{eid} 摘要", core=f"{eid} 的完整叙述",
    ))


class SedimentEvidenceTests(unittest.TestCase):
    """收证接口闭环：落账 → 追加去重 → 幻觉拒收 → 覆盖 → 重载。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.svc = _service(self.tmp_path)
        for i in range(1, 5):
            _add(self.svc, f"EVT-{i:03d}", i)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _submit(self, version: int, ids: list) -> dict:
        return self.svc.sidecar_sediment_evidence(
            {"version": version, "evidence_event_ids": ids})

    def test_appended_and_ledger(self) -> None:
        """正常落账：证据挂到版本名下，账本文件与内存一致。"""
        out = self._submit(1, ["EVT-001", "EVT-002"])
        self.assertEqual(out["status"], "appended")
        self.assertEqual(out["evidence_kept"], ["EVT-001", "EVT-002"])
        self.assertEqual(out["latest_version"], 1)
        store = self.svc.sediment_store
        self.assertEqual(store.versions[1]["evidence_event_ids"],
                         ["EVT-001", "EVT-002"])
        self.assertTrue((self.tmp_path / "data" / "sediment.jsonl").exists())

    def test_cross_version_dedup(self) -> None:
        """只追加净新增：v2 交 [旧 id, 新 id] → 只挂新的，v1 记录不动。"""
        self._submit(1, ["EVT-001"])
        out = self._submit(2, ["EVT-001", "EVT-003"])
        self.assertEqual(out["status"], "appended")
        self.assertEqual(out["evidence_kept"], ["EVT-003"])
        self.assertEqual(out["duplicates_dropped"], ["EVT-001"])
        store = self.svc.sediment_store
        self.assertEqual(store.versions[1]["evidence_event_ids"], ["EVT-001"])
        self.assertEqual(store.versions[2]["evidence_event_ids"], ["EVT-003"])
        self.assertEqual(store.known_evidence(), {"EVT-001", "EVT-003"})

    def test_in_version_dedup(self) -> None:
        """本版内重复 id 也剔掉。"""
        out = self._submit(1, ["EVT-001", "EVT-001", "EVT-002"])
        self.assertEqual(out["status"], "appended")
        self.assertEqual(out["evidence_kept"], ["EVT-001", "EVT-002"])
        self.assertEqual(out["duplicates_dropped"], ["EVT-001"])

    def test_hallucinated_id_rejected(self) -> None:
        """不存在的 event id = 幻觉，整条拒收不落账。"""
        out = self._submit(1, ["EVT-001", "EVT-999"])
        self.assertEqual(out["status"], "rejected")
        self.assertIn("幻觉", out["errors"][0])
        self.assertEqual(self.svc.sediment_store.versions, {})

    def test_bad_params_rejected(self) -> None:
        """空证据集 / version 非法 → 拒收（空集是调用方违约）。"""
        out = self._submit(1, [])
        self.assertEqual(out["status"], "rejected")
        out = self._submit(0, ["EVT-001"])
        self.assertEqual(out["status"], "rejected")
        out = self._submit("2", ["EVT-001"])
        self.assertEqual(out["status"], "rejected")
        self.assertEqual(self.svc.sediment_store.versions, {})

    def test_all_duplicates_no_row(self) -> None:
        """发来的全是历史已有证据 → 不落行，如实回报。"""
        self._submit(1, ["EVT-001"])
        out = self._submit(2, ["EVT-001"])
        self.assertEqual(out["status"], "no_new_evidence")
        store = self.svc.sediment_store
        self.assertNotIn(2, store.versions)
        self.assertEqual(store.latest_version(), 1)

    def test_same_version_resubmit_overwrites(self) -> None:
        """同版本重交覆盖为最新列表；旧列表独占 id 退出并集。"""
        self._submit(2, ["EVT-001"])
        out = self._submit(2, ["EVT-002"])
        self.assertEqual(out["status"], "appended")
        store = self.svc.sediment_store
        self.assertEqual(store.versions[2]["evidence_event_ids"], ["EVT-002"])
        self.assertEqual(store.known_evidence(), {"EVT-002"})
        # 覆盖后 EVT-001 不再算"已有"，可以重新挂
        out = self._submit(3, ["EVT-001"])
        self.assertEqual(out["evidence_kept"], ["EVT-001"])

    def test_reload_from_disk(self) -> None:
        """重开 service：版本与证据并集从账本文件完整恢复。"""
        self._submit(1, ["EVT-001", "EVT-002"])
        self._submit(2, ["EVT-003"])
        svc2 = _service(self.tmp_path)
        store = svc2.sediment_store
        self.assertEqual(sorted(store.versions), [1, 2])
        self.assertEqual(store.known_evidence(),
                         {"EVT-001", "EVT-002", "EVT-003"})
        self.assertEqual(store.latest_version(), 2)


if __name__ == "__main__":
    unittest.main()
