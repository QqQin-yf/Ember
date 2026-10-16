# -*- coding: utf-8 -*-
"""实体 summary 夜批重算（2026-10-15 momo 拍板：提炼式 + 脏标记）的验收。

此前 Entity.summary 有字段、有消费方（召回渲染）、有任务点配置，
唯独没有生产方——退役 sleep_run 后编排一直悬空，所有实体 summary
恒为空串。本文件验：归属变更打脏标记 → structure/run 收尾夜批
重算（材料只含未被推翻成员的 core）→ 写回 + 脏标记清空 + 失败不牵连。
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

NOW = "2026-10-15T00:00:00+08:00"


def _service(tmp: Path) -> EmberService:
    return EmberService(EmberStore(str(tmp / "data")), MockLLMClient(),
                        model="test")


def _add(svc: EmberService, eid: str, core: str) -> None:
    from ember.models import Event
    svc.store.write_event(Event(
        event_id=eid, version_id=f"{eid}-v1",
        source_ref=f"conversation/session-s1/turn-1",
        raw_ref=f"conversation/session-s1/turn-1",
        event_time=NOW, ingest_time=NOW,
        summary=f"{eid} 摘要", core=core,
    ))


def _mk_entity(svc: EmberService, eid: str, name: str) -> str:
    from ember.entity import Entity
    ent = Entity(entity_id=eid, name=name)
    svc.entity_store.upsert(ent)
    svc.entity_store.save()
    return eid


class SummaryNightBatchTests(unittest.TestCase):
    """夜批闭环：脏标记 → 重算（材料过滤）→ 写回 → 清空。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _service(Path(self._tmp.name))
        self.ent = _mk_entity(self.svc, "ent_aaaa", "测试实体")
        _add(self.svc, "EVT-001", "第一条：实体的开端。")
        _add(self.svc, "EVT-002", "第二条：实体的进展。")
        self.svc._patch_event_rows(
            {"EVT-001": {"entity_refs": ["ent_aaaa"]},
             "EVT-002": {"entity_refs": ["ent_aaaa"]}})
        self.captured = {}

        def fake_llm(system, user, engine, task=None, **kw):
            self.captured = {"system": system, "user": user,
                             "engine": engine, "task": task}
            return '{"summary": "测试实体是一项持续进行的事物，目前包含两条经历，最近有新进展。"}'

        self._p = mock.patch("ember.executors.call_llm", side_effect=fake_llm)
        self._mock = self._p.start()
        # 夜批补向量不真调 embedding
        self._pv = mock.patch("ember.service.embed_text",
                              return_value=[0.1, 0.2, 0.3])
        self._pv.start()

    def tearDown(self) -> None:
        self._p.stop()
        self._pv.stop()
        self._tmp.cleanup()

    def _dirty_add(self) -> None:
        self.svc._summary_dirty_add("ent_aaaa")

    def test_night_batch_updates_summary(self) -> None:
        """脏标记 → 夜批：LLM 被调（task=summary）、summary 写回、
        summary_at 更新、脏标记清空、报告如实。"""
        self._dirty_add()
        report = self.svc.run_structure_maintenance()
        out = report["summary"]
        self.assertEqual([u["entity_id"] for u in out["updated"]],
                         ["ent_aaaa"])
        ent = self.svc.entity_store.get("ent_aaaa")
        self.assertIn("持续进行的事物", ent.summary)
        self.assertTrue(ent.summary_at)
        # 脏标记被消费清空：再跑一轮不再重算
        self.captured = {}
        report2 = self.svc.run_structure_maintenance()
        self.assertEqual(report2["summary"]["updated"], [])

    def test_material_uses_core_and_excludes_superseded(self) -> None:
        """材料 = 成员 core（非 summary）；superseded 成员被剔除。"""
        _add(self.svc, "EVT-003", "第三条：被推翻的旧认知。")
        self.svc._patch_event_rows(
            {"EVT-003": {"entity_refs": ["ent_aaaa"]}})
        self.svc.sleep_state.mark("EVT-003", "superseded", "被 EVT-001 修正",
                                  "test")
        self._dirty_add()
        self.svc.run_structure_maintenance()
        user = self.captured["user"]
        self.assertIn("第一条：实体的开端", user)      # core 进了材料
        self.assertNotIn("EVT-001 摘要", user)          # 不是 summary 拼盘
        self.assertNotIn("被推翻的旧认知", user)        # superseded 剔除
        self.assertEqual(self.captured["task"], "summary")

    def test_attach_marks_dirty(self) -> None:
        """attach 落库即打脏标记（白天挂关系的实体当晚被夜批覆盖）。"""
        _add(self.svc, "EVT-010", "第十条：白天新挂进来的。")
        self.svc._apply_entity_action(
            {"action": "attach", "entity_id": "ent_aaaa"},
            ["EVT-010"], "test")
        report = self.svc.run_structure_maintenance()
        self.assertEqual([u["entity_id"] for u in
                          report["summary"]["updated"]], ["ent_aaaa"])
        user = self.captured["user"]
        self.assertIn("第十条：白天新挂进来的", user)

    def test_extract_creates_dirty_and_first_summary(self) -> None:
        """extract 新开实体 → 第一版 summary 在夜批生成。"""
        _add(self.svc, "EVT-021", "新主题事件甲。")
        _add(self.svc, "EVT-022", "新主题事件乙。")
        _add(self.svc, "EVT-023", "新主题事件丙。")
        out = self.svc._apply_entity_action(
            {"action": "extract", "name": "新主题",
             "member_event_ids": ["EVT-021", "EVT-022", "EVT-023"]},
            [], "test")
        new_eid = out["entity_id"]
        report = self.svc.run_structure_maintenance()
        ids = [u["entity_id"] for u in report["summary"]["updated"]]
        self.assertIn(new_eid, ids)
        user = self.captured["user"]
        self.assertIn("新主题事件甲", user)
        self.assertIn("新主题", user)

    def test_single_failure_does_not_cascade(self) -> None:
        """单实体 LLM 失败不牵连其他实体，failed 如实上报。"""
        _mk_entity(self.svc, "ent_bbbb", "另一个实体")
        _add(self.svc, "EVT-031", "另一实体的事件。")
        self.svc._patch_event_rows(
            {"EVT-031": {"entity_refs": ["ent_bbbb"]}})
        self._dirty_add()
        self.svc._summary_dirty_add("ent_bbbb")
        calls = {"n": 0}

        def flaky(system, user, engine, task=None, **kw):
            calls["n"] += 1
            if "另一个实体" in user:
                raise RuntimeError("boom")
            return '{"summary": "测试实体的整体描述。"}'

        self._mock.side_effect = flaky
        report = self.svc.run_structure_maintenance()
        out = report["summary"]
        self.assertEqual([u["entity_id"] for u in out["updated"]],
                         ["ent_aaaa"])
        self.assertEqual(out["failed"][0]["entity_id"], "ent_bbbb")
        self.assertIn("boom", out["failed"][0]["error"])
        # 失败的脏标记已被消费（take 即清），下轮不再重试——如实暴露
        self.assertFalse((Path(self._tmp.name) / "data"
                          / "summary_dirty.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
