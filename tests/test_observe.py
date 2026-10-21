# -*- coding: utf-8 -*-
"""观察式记忆（2026-10-16 设计夜定稿 ④observe）的验收。

覆盖：游标与门槛（不够不调 LLM、游标不推进）/ span 覆盖过滤 /
提炼落库（origin=observe、无 core、引文逐字核对剔除编造、fill
turn_span、进 link pending、当场建向量、游标推进）/
判联转正（relation → affirmed；neighbor 不动）/ 夜批合并
（高相似聚 → LLM 合并 → 旧观察 superseded、证据继承）。

外部依赖全 mock：embed_text（ember.service）、call_llm
（ember.executors）、fetch_source_path（ember.source_client）。
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

from ember.llm import MockLLMClient
from ember.models import Event, PresentationContext, RecallTrace
from ember.service import EmberService
from ember.store import EmberStore

SID = "main"
USER_LINE = "我又是拖到最后一刻才动手，总是这样。"


def _msgs(n_turns: int) -> list[dict]:
    """seq 1..2n 交替 user/assistant；user 行都带同一句可摘的原话。"""
    out = []
    for t in range(1, n_turns + 1):
        out.append({"message_id": 2 * t - 1, "role": "user",
                    "content": f"第{t}轮。{USER_LINE}"})
        out.append({"message_id": 2 * t, "role": "assistant",
                    "content": f"AI第{t}轮回复：好的，我记下了。"})
    return out


class ObserveTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = EmberService(
            EmberStore(str(Path(self._tmp.name) / "data")),
            MockLLMClient(), model="test")
        # 会话映射：turn t -> user seq 2t-1 / reply seq 2t
        for t in range(1, 220):
            self.svc.turn_ledger.record(SID, t, user_message_id=2 * t - 1)
            self.svc.turn_ledger.record_reply(SID, t, 2 * t)
        self.msgs = _msgs(219)

        # mock 外部依赖
        self._ps = [mock.patch("ember.service.embed_text",
                               side_effect=lambda text: [1.0, 0.0])]
        self._ps.append(mock.patch.object(
            __import__("ember.source_client", fromlist=["SOURCE_URL"]),
            "SOURCE_URL", "http://test"))
        self._ps.append(mock.patch(
            "ember.source_client.fetch_source_path",
            side_effect=lambda sid, ids, **kw:
                [m for m in self.msgs if str(m["message_id"]) in ids]))
        self._ps.append(mock.patch("ember.service.generation_messages", side_effect=lambda *a, **kw:
            [{"turn": (m["message_id"] + 1) // 2, "role": m["role"], "message_id": str(m["message_id"])}
             for m in self.msgs]))
        self.llm_raw = '{"observations": []}'
        self._ps.append(mock.patch(
            "ember.executors.call_llm",
            side_effect=lambda *a, **k: self.llm_raw))
        for p in self._ps:
            p.start()

    def tearDown(self) -> None:
        for p in self._ps:
            p.stop()
        self._tmp.cleanup()

    def _cover_first_turns(self, hi: int) -> None:
        """造一个已写入事件并 fill span，把前 hi 轮标记为已覆盖。"""
        eid = "EVT-001-cccccc"
        self.svc.store.write_event(Event(
            event_id=eid, version_id=f"{eid}-v1",
            source_ref=f"conversation/session-{SID}",
            raw_ref=f"conversation/session-{SID}/turn-1",
            event_time="2026-10-14T00:00:00+08:00",
            ingest_time="2026-10-14T00:00:00+08:00",
            summary="已写成事件的经历", core="已写成事件的经历"))
        self.svc.turn_spans.fill(eid, [(1, hi)], filled_by="test")

    def test_below_threshold_skips_llm_and_keeps_cursor(self) -> None:
        """未覆盖不足 300：不调 LLM、不落事件、游标不动。"""
        # 219 轮 = 438 条；盖掉前 139 轮（278 条）→ 未覆盖 160 < 300
        self._cover_first_turns(139)
        report = self.svc.run_observe_pass()
        session = report["sessions"][0]
        self.assertEqual(session["status"], "below_threshold",
                         str(session))
        self.assertEqual(report["created"], 0)
        self.assertEqual(self.svc.observe_cursor.get(SID), 0)
        self.assertEqual(len(self.svc.store.all_events()), 1)

    def test_extraction_creates_observe_events(self) -> None:
        """达门槛 → 提炼落库：origin/无 core/引文核对/span/pending/向量/游标。"""
        self._cover_first_turns(2)          # 盖 4 条 → 438-4 = 434 未覆盖
        self.llm_raw = json.dumps({"observations": [
            {"summary": "用户习惯拖到最后一刻才动手",
             "quotes": [{"speaker": "user", "text": USER_LINE},
                        {"speaker": "user", "text": "这句原话不存在。"}]},
            {"summary": "", "quotes": []},   # 空 summary → 跳过
        ]}, ensure_ascii=False)
        report = self.svc.run_observe_pass()
        session = report["sessions"][0]
        self.assertEqual(session["status"], "ok", str(session))
        self.assertEqual(report["created"], 1)
        eid = session["event_ids"][0]
        ev = self.svc.store.get_event(eid)
        self.assertEqual(ev.origin, "observe")
        self.assertEqual(ev.core, "")                    # 观察无 core
        self.assertIn("拖到最后一刻", ev.summary)
        self.assertEqual(len(ev.key_quotes), 1)          # 编造引文被剔
        self.assertEqual(ev.key_quotes[0]["speaker"], "user")
        self.assertIn("seq-", ev.raw_ref)                # 区间指针
        self.assertTrue(self.svc.turn_spans.span_of(eid))
        self.assertIn(eid, self.svc._link_pending_list())
        self.assertTrue(self.svc.core_vectors.has(eid))  # 当场建向量
        self.assertGreater(self.svc.observe_cursor.get(SID), 0)
        # 游标已推进到批尾：再跑 → 只剩批后尾巴（不足门槛滚存）
        report2 = self.svc.run_observe_pass()
        self.assertEqual(report2["sessions"][0]["status"], "below_threshold")

    def test_zero_observations_still_advances_cursor(self) -> None:
        """提炼成功但 0 条（不硬挖）→ 游标照常推进。"""
        self.llm_raw = '{"observations": []}'
        report = self.svc.run_observe_pass()
        self.assertEqual(report["created"], 0)
        self.assertGreater(self.svc.observe_cursor.get(SID), 0)
        self.assertEqual(self.svc.run_observe_pass()["sessions"][0]
                         ["status"], "below_threshold")

    def test_affirmation_via_link_relation(self) -> None:
        """判联判出 relation = 印证 → origin 转 affirmed；neighbor 不动。"""
        eid = self._make_observe_event("OBS-1")
        material = {"new_id": eid,
                    "new_event": {"event_id": eid, "core": "", "summary": "x"},
                    "old_events": [{"event_id": "EVT-OLD-1", "core": "旧事",
                                    "summary": "旧事"}]}
        verdict = {"pairs": [{"old_event_id": "EVT-OLD-1",
                              "outcome": "relation", "type": "related_to",
                              "confidence": "high", "reason": "印证"}],
                   "entity": {"action": "none"}}
        self.svc._apply_link_verdict(verdict, material)
        self.assertEqual(self.svc.store.get_event(eid).origin, "affirmed")

        eid2 = self._make_observe_event("OBS-2")
        material2 = {"new_id": eid2,
                     "new_event": {"event_id": eid2, "core": "", "summary": "y"},
                     "old_events": [{"event_id": "EVT-OLD-1", "core": "旧事",
                                     "summary": "旧事"}]}
        verdict2 = {"pairs": [{"old_event_id": "EVT-OLD-1",
                               "outcome": "neighbor", "reason": "只是邻居"}],
                    "entity": {"action": "none"}}
        self.svc._apply_link_verdict(verdict2, material2)
        self.assertEqual(self.svc.store.get_event(eid2).origin, "observe")

    def test_merge_high_similarity(self) -> None:
        """高相似观察 → LLM 判合并 → 新观察落库、旧的标 superseded。"""
        a = self._make_observe_event("A", summary="用户总在对齐后才动手",
                                     vec=[1.0, 0.0])
        b = self._make_observe_event("B", summary="用户习惯先对齐方案再行动",
                                     vec=[0.98, 0.01])
        c = self._make_observe_event("C", summary="完全不同的另一条观察",
                                     vec=[0.0, 1.0])
        self.llm_raw = json.dumps({"merge": True,
                                   "summary": "用户倾向先对齐方案再开始行动",
                                   "reason": "两条说同一件事"},
                                  ensure_ascii=False)
        report = self.svc._merge_observations()
        self.assertEqual(report["merged"], 1)
        # 旧两条被 superseded（召回不再带出），第三条不受影响
        self.assertFalse(self.svc.sleep_state.recall_allowed(a))
        self.assertFalse(self.svc.sleep_state.recall_allowed(b))
        self.assertTrue(self.svc.sleep_state.recall_allowed(c))
        # 合并版：origin=observe、证据继承、进 pending
        merged = [e for e in self.svc.store.all_events()
                  if e.provenance_refs and e.provenance_refs[0] == "observe-merge"]
        self.assertEqual(len(merged), 1)
        me = merged[0]
        self.assertEqual(me.origin, "observe")
        self.assertIn("先对齐", me.summary)
        self.assertEqual(len(me.provenance_refs), 3)   # 来源 + 两条旧 id
        self.assertIn(me.event_id, self.svc._link_pending_list())

    def test_extra_hit_semantic_with_filters(self) -> None:
        """附加席：语义宽松命中；superseded/已在主席/低于阈值都不出。"""
        a = self._make_observe_event("A", summary="用户习惯先对齐再动手",
                                     vec=[1.0, 0.0])
        # 命中 A
        self.assertEqual(self.svc._observe_extra_hit("聊聊做事方式", set()), a)
        # 已在本轮主席 → 排除
        self.assertIsNone(self.svc._observe_extra_hit("聊聊做事方式", {a}))
        # superseded（被合并）→ 不再出
        self.svc.sleep_state.mark(a, "superseded", "被合并", "test")
        self.assertIsNone(self.svc._observe_extra_hit("聊聊做事方式", set()))
        # affirmed 照样可命中
        b = self._make_observe_event("B", summary="用户常深夜干活",
                                     vec=[0.99, 0.02])
        self.svc._patch_event_rows({b: {"origin": "affirmed"}},
                                   whitelist=("origin",))
        self.assertEqual(self.svc._observe_extra_hit("深夜干活的事", set()), b)
        # 无向量的观察不参与
        c = self._make_observe_event("C")
        self.svc._patch_event_rows({c: {"origin": "observe"}},
                                   whitelist=("origin",))
        self.assertEqual(self.svc._observe_extra_hit("深夜干活的事", {b}), None)

    def test_prefix_renders_observe_extra(self) -> None:
        """附加席渲染：「（印象浮现）」弱标注一行。"""
        eid = self._make_observe_event("D", summary="用户喜欢先对齐再动手")
        ctx = PresentationContext(
            recalled_events=[eid],
            recall_trace=RecallTrace(input_text="x", anchors=[]))
        p = self.svc._build_recall_prefix(
            ctx, units=[{"kind": "observe", "event_ids": [eid]}])
        self.assertIn("（印象浮现）用户喜欢先对齐再动手", p)
        self.assertIn(eid, p)

    # ---- helpers ----

    def _make_observe_event(self, tag: str, summary: str = "一条观察",
                            vec: list[float] | None = None) -> str:
        eid = f"EVT-{tag}-{tag.lower()}"
        self.svc.store.write_event(Event(
            event_id=eid, version_id=f"{eid}-v1",
            source_ref=f"conversation/session-{SID}",
            raw_ref=f"conversation/session-{SID}/seq-1-10",
            event_time="2026-10-14T00:00:00+08:00",
            ingest_time="2026-10-14T00:00:00+08:00",
            summary=summary, origin="observe",
            key_quotes=[{"speaker": "user", "text": f"{tag}的证据原话"}]))
        if vec is not None:
            self.svc.core_vectors.upsert(eid, summary, vec, "test")
        return eid


if __name__ == "__main__":
    unittest.main()
