# -*- coding: utf-8 -*-
"""结构维护（2026-10-14 设计稿 v2：第三方 LLM 判归属 + cue 收紧）的验收。

双 LLM 路线（momo 拍板）：正主只判联，归属判定外包第三方 LLM
（executors.call_llm task="structure"）——本文件 mock 掉 call_llm，
验编排闭环（清单收集 → LLM → 校验 → 落库 → cue 收紧）与校验纪律。
LLM 不可真调：所有用例的 verdict 都是固定 JSON。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.entity import Entity
from ember.prompts import STRUCTURE_SYSTEM_PROMPT
from ember.service import EmberService
from ember.store import EmberStore, EVENTS_FILE
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


def _jsonl_refs(data_dir: Path) -> dict[str, list]:
    p = data_dir / EVENTS_FILE
    out: dict[str, list] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            d = json.loads(line)
            out[d["event_id"]] = list(d.get("entity_refs") or [])
    return out


class StructureMaintenanceTests(unittest.TestCase):
    """结构维护编排：散事件清单 → 第三方 LLM → 逐条校验落库 → cue 收紧。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "data"
        self.svc = _service(Path(self._tmp.name))
        for i, eid in enumerate(["EVT-A1", "EVT-A2", "EVT-A3"]):
            _add(self.svc, eid, i + 1)
        self.captured: dict = {}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, verdict_text: str) -> dict:
        """mock 第三方 LLM 输出，跑一遍结构维护。"""

        def fake_call_llm(system, user, engine, task="judge"):
            self.captured.update(system=system, user=user,
                                 engine=engine, task=task)
            return verdict_text

        with mock.patch("ember.executors.call_llm",
                        side_effect=fake_call_llm):
            out = self.svc.run_structure_maintenance()
        return out

    def test_extract_creates_entity_and_patches_refs(self) -> None:
        """extract：立新户 + 三条散事件入户新实体（jsonl 行 + 注册表双源）。"""
        verdict = json.dumps({"assignments": [
            {"action": "extract", "name": "Ember记忆系统",
             "aliases": ["Ember"], "description": "记忆系统项目",
             "member_event_ids": ["EVT-A1", "EVT-A2", "EVT-A3"]}]},
            ensure_ascii=False)
        out = self._run(verdict)
        self.assertEqual(out["strays_total"], 3)
        self.assertEqual(len(out["saved"]), 1)
        self.assertEqual(out["rejected"], [])
        saved = out["saved"][0]
        self.assertTrue(saved["entity_id"].startswith("ent_"))
        self.assertEqual(saved["entity_action"],
                         f"extract:{saved['entity_id']}"
                         f"(Ember记忆系统) x3")
        # 实体库落户新实体
        ents = {e.entity_id: e for e in self.svc.entity_store.all()}
        self.assertIn(saved["entity_id"], ents)
        self.assertEqual(ents[saved["entity_id"]].name, "Ember记忆系统")
        self.assertEqual(ents[saved["entity_id"]].aliases, ["Ember"])
        # jsonl 行级回填 entity_refs（dataclass 写入会丢，行级 patch 补）
        refs = _jsonl_refs(self.data_dir)
        for eid in ("EVT-A1", "EVT-A2", "EVT-A3"):
            self.assertEqual(refs[eid], [saved["entity_id"]])
        # cue 收紧照跑
        self.assertGreaterEqual(out["cue_tighten"]["checked_events"], 3)

    def test_attach_to_existing_entity(self) -> None:
        """attach：散事件单归属挂入已有实体。"""
        self.svc.entity_store.upsert(
            Entity(entity_id="ent_test", name="已有实体"))
        self.svc.entity_store.save()
        verdict = json.dumps({"assignments": [
            {"action": "attach", "entity_id": "ent_test",
             "event_ids": ["EVT-A1", "EVT-A2"]}]}, ensure_ascii=False)
        out = self._run(verdict)
        self.assertEqual(len(out["saved"]), 1)
        self.assertEqual(out["saved"][0]["entity_action"], "attach:ent_test")
        self.assertEqual(out["saved"][0]["members_patched"], 2)
        refs = _jsonl_refs(self.data_dir)
        self.assertEqual(refs["EVT-A1"], ["ent_test"])
        self.assertEqual(refs["EVT-A2"], ["ent_test"])
        self.assertEqual(refs["EVT-A3"], [])      # 没点的保持散着
        # 实体未判收尾：状态不动
        self.assertEqual(self.svc.entity_store.get("ent_test").status, "open")

    def test_no_strays_skips_llm(self) -> None:
        """无散事件：跳过 LLM，只跑 cue 收紧。"""
        svc = _service(Path(self._tmp.name) / "empty")
        with mock.patch("ember.executors.call_llm") as m:
            out = svc.run_structure_maintenance()
        m.assert_not_called()
        self.assertEqual(out["strays_total"], 0)
        self.assertEqual(out["saved"], [])
        self.assertIn("checked_events", out["cue_tighten"])

    def test_rejects_hallucinated_member(self) -> None:
        """防幻觉：member 指向清单外事件 → 拒绝该条，不落库。"""
        verdict = json.dumps({"assignments": [
            {"action": "extract", "name": "幽灵实体", "aliases": [],
             "description": "编的",
             "member_event_ids": ["EVT-GHOST", "EVT-A1", "EVT-A2"]}]},
            ensure_ascii=False)
        out = self._run(verdict)
        self.assertEqual(out["saved"], [])
        self.assertEqual(len(out["rejected"]), 1)
        self.assertTrue(any("EVT-GHOST" in e
                            for e in out["rejected"][0]["errors"]))
        self.assertFalse(any(e.name == "幽灵实体"
                             for e in self.svc.entity_store.all()))
        refs = _jsonl_refs(self.data_dir)
        self.assertEqual(refs["EVT-A1"], [])      # 未被牵连

    def test_single_ownership_across_assignments(self) -> None:
        """单归属铁律：同一事件被两个动作认领 → 第二个拒绝。"""
        self.svc.entity_store.upsert(
            Entity(entity_id="ent_test", name="已有实体"))
        self.svc.entity_store.save()
        verdict = json.dumps({"assignments": [
            {"action": "extract", "name": "新主题", "aliases": [],
             "description": "x",
             "member_event_ids": ["EVT-A1", "EVT-A2", "EVT-A3"]},
            {"action": "attach", "entity_id": "ent_test",
             "event_ids": ["EVT-A3"]}]}, ensure_ascii=False)
        out = self._run(verdict)
        self.assertEqual(len(out["saved"]), 1)    # extract 落
        self.assertEqual(len(out["rejected"]), 1)  # attach 拒
        self.assertTrue(any("单归属" in e
                            for e in out["rejected"][0]["errors"]))
        refs = _jsonl_refs(self.data_dir)
        self.assertNotEqual(refs["EVT-A3"], ["ent_test"])

    def test_rerun_idempotent(self) -> None:
        """幂等：跑过一遍后重跑，已归属的自动出清单，LLM 看不到。"""
        first = json.dumps({"assignments": [
            {"action": "extract", "name": "Ember记忆系统", "aliases": [],
             "description": "x",
             "member_event_ids": ["EVT-A1", "EVT-A2", "EVT-A3"]}]},
            ensure_ascii=False)
        self._run(first)
        out2 = self._run(json.dumps({"assignments": []}))
        self.assertEqual(out2["strays_total"], 0)
        self.assertEqual(out2["saved"], [])
        self.assertEqual(out2["rejected"], [])

    def test_llm_call_contract(self) -> None:
        """第三方调用契约：task=structure、system 用 STRUCTURE prompt、
        user 含实体库摘要 + 散事件清单。"""
        self.svc.entity_store.upsert(
            Entity(entity_id="ent_test", name="已有实体",
                   summary="一个已有实体"))
        self.svc.entity_store.save()
        self._run(json.dumps({"assignments": []}))
        self.assertEqual(self.captured["task"], "structure")
        self.assertEqual(self.captured["system"], STRUCTURE_SYSTEM_PROMPT)
        self.assertIn("EVT-A1", self.captured["user"])
        self.assertIn("ent_test 已有实体（open）一个已有实体",
                      self.captured["user"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
