# -*- coding: utf-8 -*-
"""睡眠收尾维护（structure/run，2026-10-17 重构）的验收。

2026-10-17 起 structure/run 只剩本地+轻活：cue 收紧 + 向量补建
+ 观察提炼与合并。归属判定（extract/attach + entity summary 当场
写改）已收编窗口判联——本文件验：报告形态、归属 LLM 确实不再被
调用、收紧与补建照常。
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

NOW = "2026-10-14T00:00:00+08:00"


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


class StructureRunTests(unittest.TestCase):
    """structure/run 瘦身后：四步收尾，归属 LLM 退役。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _service(Path(self._tmp.name))
        for i, eid in enumerate(["EVT-A1", "EVT-A2", "EVT-A3"]):
            _add(self.svc, eid, i + 1)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_report_shape_and_no_attribution_llm(self) -> None:
        """报告 = 四步收尾；散事件在场也不调归属 LLM（已退役）。"""
        with mock.patch("ember.executors.call_llm") as m, \
                mock.patch("ember.service.embed_text",
                           return_value=[0.1, 0.2, 0.3]), \
                mock.patch("ember.source_client.SOURCE_URL", ""):
            out = self.svc.run_structure_maintenance()
        m.assert_not_called()   # 归属判定/summary 重算已退役
        self.assertEqual(set(out), {"cue_tighten", "vector_backfill",
                                    "observe", "observe_merge"})
        self.assertNotIn("strays_total", out)
        self.assertNotIn("summary", out)
        self.assertGreaterEqual(out["cue_tighten"]["checked_events"], 3)
        self.assertEqual(out["vector_backfill"]["events"], 3)
        self.assertEqual(out["observe"]["status"], "unconfigured")

    def test_rerun_idempotent(self) -> None:
        """幂等：向量补建只补一次，收紧照跑。"""
        with mock.patch("ember.service.embed_text",
                        return_value=[0.1, 0.2, 0.3]):
            first = self.svc.run_structure_maintenance()
            second = self.svc.run_structure_maintenance()
        self.assertEqual(first["vector_backfill"]["events"], 3)
        self.assertEqual(second["vector_backfill"]["events"], 0)
        self.assertEqual(second["cue_tighten"]["checked_events"], 3)


if __name__ == "__main__":
    unittest.main()
