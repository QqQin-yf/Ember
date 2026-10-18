# -*- coding: utf-8 -*-
"""简报（2026-10-17 设计夜：截断驱动，拼装式零 LLM）与钉住的验收。

覆盖：
1. PinStore：钉住/取消/幂等/持久化
2. significance 数字格式校验（写入端）
3. briefing/refresh 拼装：分层（原文/core/summary）、现场排除、
   观察排除、类间去重、召回最相关一条、72h 窗口、预算砍低层
4. 简报注入记在场（ledger route=briefing → 2h 轻量联动）
5. read_memory_source 上下文扩展（单句 ±3 轮 / 真区间按区间）
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.pin_store import PinStore
from ember.service import EmberService
from ember.store import EmberStore
from ember.llm import MockLLMClient
from ember.writer import validate_writer_arguments

NOW = datetime.now(timezone.utc).astimezone()
OLD = (NOW - timedelta(days=10)).isoformat(timespec="seconds")


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class PinStoreTests(unittest.TestCase):

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = PinStore(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_add_remove_idempotent(self) -> None:
        self.assertTrue(self.store.add("EVT-A"))
        self.assertFalse(self.store.add("EVT-A"))      # 重复钉 → 无新动作
        self.assertTrue(self.store.is_pinned("EVT-A"))
        self.assertTrue(self.store.remove("EVT-A"))
        self.assertFalse(self.store.remove("EVT-A"))   # 再取消 → 无动作
        self.assertFalse(self.store.is_pinned("EVT-A"))

    def test_persist(self) -> None:
        self.store.add("EVT-A")
        self.store.add("EVT-B")
        again = PinStore(self._tmp.name)
        self.assertEqual(again.all(), ["EVT-A", "EVT-B"])


class SignificanceFormatTests(unittest.TestCase):
    """significance 数字化（简报分层依据，格式必须可靠）。"""

    def _evt(self, sig: str) -> dict:
        return {"event": {
            "summary": "s", "participants": ["user", "ai"], "objects": [],
            "context": "c", "core": "c", "outcome": "o", "state": "s",
            "affect": "a", "significance": sig, "details": [],
            "key_quotes": [],
        }, "recall_routes": [{"cue_type": "lexical", "cue": "触"}],
            "quote_spans": [{"start_quote": "起句在这里",
                             "end_quote": "尾句在这里"}]}

    def test_valid_formats(self) -> None:
        for sig in ("8：Deliz 开发线的转折", "10:永久要记住的", "5：背景"):
            out, err = validate_writer_arguments(self._evt(sig))
            self.assertIsNone(err, sig)
            self.assertIsNotNone(out, sig)

    def test_invalid_formats_rejected(self) -> None:
        for sig in ("重要发现", "11：超纲", "0：零", "8 没有冒号"):
            out, err = validate_writer_arguments(self._evt(sig))
            self.assertIsNone(out, sig)
            self.assertIn("significance", err or "")


def _service(tmp: Path) -> EmberService:
    return EmberService(EmberStore(str(tmp / "data")), MockLLMClient(),
                        model="test")


class BriefingTests(unittest.TestCase):
    """refresh 拼装：分层、现场排除、观察排除、去重、最相关、预算。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _service(Path(self._tmp.name))
        # 原文语料：按请求轮次合成（source 未配置环境，走内存合成通道）
        self.svc._fetched_corpus = (  # noqa: SLF001 — 测试直接替换
            lambda sid, turns: [{"turn": t, "role": "user",
                                 "content": f"原文{t}"}
                                for t in sorted(turns)])
        s = self.svc
        self._add("EVT-PIN", 1, "10：钉住的重大决定", OLD, pin=True)
        self._add("EVT-S10", 2, "10：十个分", OLD)
        self._add("EVT-C9", 3, "9：九分进展", _iso(NOW))
        self._add("EVT-C8", 4, "8：八分进展", _iso(NOW))
        self._add("EVT-S6", 5, "6：六分背景", _iso(NOW))
        self._add("EVT-S2", 6, "2：两分琐碎", _iso(NOW))
        self._add("EVT-OLDN", 7, "7：过期新增", OLD)          # 72h 外
        self._add("EVT-STAGE", 50, "5：现场内", _iso(NOW))    # 现场
        self._add("EVT-OBS", 8, "", _iso(NOW), origin="observe")
        self._add("EVT-R1", 9, "6：被召回两次", OLD)
        self._add("EVT-R2", 10, "6：被召回一次", OLD)
        s.recall_ledger.record("s1", 21, "cue", ["EVT-R1"], cue="a")
        s.recall_ledger.record("s1", 22, "cue", ["EVT-R1"], cue="a")
        s.recall_ledger.record("s1", 23, "cue", ["EVT-R2"], cue="b")
        # 账本铺垫：turn 40 的 user 行 id = 800（keep_from_message_id
        # 经 turn_ledger 换算成起点轮，app 不接触内部 turn 坐标）
        s.turn_ledger.record("s1", 40, None, user_message_id=800)
        self.args = {"session_id": "s1", "keep_from_message_id": 800}

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, eid: str, turn: int, sig: str, ingest: str,
             pin: bool = False, origin: str = "") -> None:
        from ember.models import Event
        ev = Event(
            event_id=eid, version_id=f"{eid}-v1",
            source_ref="conversation/session-s1",
            raw_ref=f"conversation/session-s1/turn-{turn}",
            event_time=ingest, ingest_time=ingest,
            turn_refs=[f"turn-{turn}"],
            summary=f"{eid} 的摘要", core=f"{eid} 的完整叙述",
            significance=sig or "5：无标分", origin=origin,
            key_quotes=[{"speaker": "user", "text": f"{eid} 的原话"}],
        )
        self.svc.store.write_event(ev)
        self.svc.turn_spans.fill(eid, [(turn, turn)], filled_by="test")
        if pin:
            self.svc.pin_store.add(eid)

    def _refresh(self) -> dict:
        return self.svc.sidecar_briefing_refresh(self.args)

    def test_layering_and_exclusions(self) -> None:
        out = self._refresh()
        self.assertEqual(out["status"], "ok")
        text = out["text"]
        # 无标题行（2026-10-18 momo 拍板）：text 是纯条目列表，
        # 标题由 app 注入时自己加
        self.assertFalse(text.startswith("【"), text[:20])
        self.assertIn("EVT-PIN 的完整叙述", text)
        self.assertIn("【原文】", text)
        self.assertIn("原文1", text)
        self.assertIn("EVT-S10 的完整叙述", text)
        # core 层：8-9 新增 + 召回最相关一条
        self.assertIn("EVT-C9 的完整叙述", text)
        self.assertIn("EVT-C8 的完整叙述", text)
        self.assertIn("EVT-R1 的完整叙述", text)
        # summary 层：1-7 新增
        self.assertIn("EVT-S6 的摘要", text)
        self.assertIn("EVT-S2 的摘要", text)
        # 排除：现场 / 观察事件 / 72h 外 / 非最相关召回
        self.assertNotIn("EVT-STAGE", text)
        self.assertNotIn("EVT-OBS", text)
        self.assertNotIn("EVT-OLDN", text)
        self.assertNotIn("EVT-R2", text)
        # 类间去重：钉住兼 10 分只出现一次
        self.assertEqual(text.count("EVT-PIN 的完整叙述"), 1)
        # 注入记在场（ledger route=briefing）
        self.assertIn(("s1", "EVT-C9"), self.svc.recall_ledger.last_injected)
        self.assertEqual(out["stats"]["recalled_top"], "EVT-R1")
        # 持久化 + get
        again = self.svc.sidecar_briefing_get()
        self.assertEqual(again["text"], text)

    def test_keep_from_message_id_via_ledger(self) -> None:
        """现场起点收 message_id：账本换算三态（精确/空隙/换算不到）。"""
        s = self.svc
        s.turn_ledger.record("s1", 30, None, user_message_id=600)
        s.turn_ledger.record_reply("s1", 30, assistant_message_id=640)
        s.turn_ledger.record_reply("s1", 40, assistant_message_id=840)
        s.turn_ledger.record("s1", 50, None, user_message_id=1000)
        s.turn_ledger.record_reply("s1", 50, assistant_message_id=1040)
        # 精确归属：800 → turn 40，现场（50）不进
        out = s.sidecar_briefing_refresh(
            {"session_id": "s1", "keep_from_message_id": 800})
        self.assertEqual(out["status"], "ok")
        self.assertNotIn("EVT-STAGE", out["text"])
        # 两轮之间的空隙：650 → 其后最早一轮（40）
        out = s.sidecar_briefing_refresh(
            {"session_id": "s1", "keep_from_message_id": 650})
        self.assertNotIn("EVT-STAGE", out["text"])
        # 换算不到（消息没进过账本）→ 0 = 不排除现场，保守多带
        out = s.sidecar_briefing_refresh(
            {"session_id": "s1", "keep_from_message_id": 99999})
        self.assertIn("EVT-STAGE", out["text"])

    def test_onstage_uses_span_not_birth_stamp(self) -> None:
        """现场判定用经历区间（turn_spans），不用出生图章（turn_refs）。

        turn_span.py 2026-10-12 裁决：出生时刻不冒充经历时刻——
        截断后补写的记忆出生在保留窗口内、经历却在已截掉的旧轮，
        必须进简报（momo 实测踩中的 bug）。跨会话的事件原文不在
        本会话上下文里，同样不算现场。
        """
        # 出生在 turn 45（保留窗口内），经历在 turn 5-6（已截掉）
        self._add("EVT-LATE", 45, "9：截断后补写的旧事", _iso(NOW))
        self.svc.turn_spans.fill("EVT-LATE", [(5, 6)], filled_by="test")
        # 别的会话写的事件，span 伸进本会话保留窗口也不算现场
        from ember.models import Event
        self.svc.store.write_event(Event(
            event_id="EVT-OTHER", version_id="EVT-OTHER-v1",
            source_ref="conversation/session-s2",
            raw_ref="conversation/session-s2/turn-50",
            event_time=_iso(NOW), ingest_time=_iso(NOW),
            turn_refs=["turn-50"],
            summary="别会话的摘要", core="别会话的完整叙述",
            significance="7：别的会话",
            key_quotes=[{"speaker": "user", "text": "别会话的原话"}],
        ))
        self.svc.turn_spans.fill("EVT-OTHER", [(50, 50)], filled_by="test")
        out = self._refresh()   # keep_from_message_id=800 → turn 40
        # 出生图章在窗口内、经历在旧轮 → 进（core 层，sig 9）
        self.assertIn("EVT-LATE 的完整叙述", out["text"])
        # 跨会话 → 不参与现场排除，进（summary 层）
        self.assertIn("别会话的摘要", out["text"])
        # 同会话、经历区间（50）真在窗口内 → 仍然排除（EVT-STAGE）
        self.assertNotIn("EVT-STAGE", out["text"])

    def test_briefing_presence_not_counted_as_recall(self) -> None:
        """简报在场记账（route=briefing）不算被召回（2026-10-17 修）。

        反复 refresh 不得把进过简报的事件顶上 recalled_top——
        真被召回两次的 EVT-R1 必须守住席位。
        """
        for _ in range(3):
            self._refresh()
        # 进过简报的事件没有被召回计数
        self.assertFalse(self.svc.recall_ledger.event_recall_count.get("EVT-C9"))
        # 在场去重账照记（2h 轻量联动的依据，不受本修复影响）
        self.assertIn(("s1", "EVT-C9"),
                      self.svc.recall_ledger.last_injected)
        # recalled_top 仍是真召回最多的 EVT-R1，不被刷上去的挤掉
        out = self._refresh()
        self.assertEqual(out["stats"]["recalled_top"], "EVT-R1")

    def test_keep_from_not_swallowed_by_reply_less_turn(self) -> None:
        """缺 reply 的轮不得当无上界区间（2026-10-17 修）。

        生产实测：早期轮 chat 失败没记 reply → 区间成 [um, ∞) 开口，
        晚期消息 id（截断起点）被吞进古老轮，keep_from 换算到 turn
        5-12，全部记忆被误判现场。
        """
        s = self.svc
        # turn 6：user=45 已记、reply 缺（chat 失败/turn_end 被拒收）
        s.turn_ledger.record("s1", 6, None, user_message_id=45)
        # turn 41：正常轮 [100, 101]
        s.turn_ledger.record("s1", 41, None, user_message_id=100)
        s.turn_ledger.record_reply("s1", 41, assistant_message_id=101)
        # 晚期起点 100/101 → 落 turn 41，不被 turn 6 的开口吞掉
        self.assertEqual(s.turn_ledger.turn_at_or_after("s1", 100), 41)
        self.assertEqual(s.turn_ledger.turn_at_or_after("s1", 101), 41)
        # 在飞轮自己的 user 行仍认（45 → turn 6）
        self.assertEqual(s.turn_ledger.turn_at_or_after("s1", 45), 6)
        self.assertEqual(s.turn_ledger.turn_of_message("s1", 45), 6)
        # 开口区间内的归属不明 id：起点走空隙规则（其后最早一轮），
        # 精确归属如实 None
        self.assertEqual(s.turn_ledger.turn_at_or_after("s1", 60), 40)
        self.assertIsNone(s.turn_ledger.turn_of_message("s1", 60))

    def test_entries_carry_absolute_when(self) -> None:
        """条目带绝对时间标注（2026-10-18，给 app 的 AI 看）。

        event_time 渲染为 [MM-DD] 前缀（跨年带年份）；缺失 → 无前缀。
        """
        from datetime import date
        out = self._refresh()
        today = date.today()
        # 原文层与 core/summary 层条目均带 [MM-DD]（regex 断言，
        # 跨月/跨年边界由日期串自证，不写死天数）
        self.assertRegex(out["text"], r"◆ \[\d{2}-\d{2}\]")
        self.assertRegex(out["text"], r"• \[\d{2}-\d{2}\]")
        # 跨年事件带完整年份（OLD 是 NOW-10 天，构造跨年场景）
        from ember.models import Event
        last_year = (NOW - timedelta(days=400)).isoformat(timespec="seconds")
        self.svc.store.write_event(Event(
            event_id="EVT-YEAR", version_id="EVT-YEAR-v1",
            source_ref="conversation/session-s1",
            raw_ref="conversation/session-s1/turn-20",
            event_time=last_year, ingest_time=_iso(NOW),
            turn_refs=["turn-20"],
            summary="去年的事", core="去年的完整叙述",
            significance="9：去年的重要事",
            key_quotes=[{"speaker": "user", "text": "去年的原话"}],
        ))
        self.svc.turn_spans.fill("EVT-YEAR", [(20, 20)], filled_by="test")
        out = self._refresh()
        # 跨年 → [YYYY-MM-DD]；无 event_time 的事件 → 无前缀
        self.assertRegex(out["text"], r"• \[\d{4}-\d{2}-\d{2}\] 去年的完整叙述")

    def test_budget_cuts_low_layers_first(self) -> None:
        self.svc.BRIEFING_BUDGET = 120   # 原文层就超——可砍层全砍，pin/10 保留
        out = self._refresh()
        # pin/10 永不砍
        self.assertIn("EVT-PIN 的完整叙述", out["text"])
        self.assertIn("EVT-S10 的完整叙述", out["text"])
        # 低层被砍光
        self.assertGreaterEqual(out["stats"]["dropped"]["core"], 1)
        self.assertNotIn("EVT-C9 的完整叙述", out["text"])
        self.assertNotIn("EVT-S6 的摘要", out["text"])


class ReadContextTests(unittest.TestCase):
    """read_memory_source 上下文扩展：单句 ±3 轮，真区间按区间。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = _service(Path(self._tmp.name))
        self.captured: list[set] = []

        def fake_corpus(sid, turns):
            self.captured.append(set(turns))
            return [{"turn": t, "role": "user", "content": f"原文{t}"}
                    for t in sorted(turns)]

        self.svc._fetched_corpus = fake_corpus  # noqa: SLF001 — 测试替换

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _add(self, eid: str, spans: list[tuple[int, int]]) -> None:
        from ember.models import Event
        self.svc.store.write_event(Event(
            event_id=eid, version_id=f"{eid}-v1",
            source_ref="conversation/session-s1",
            raw_ref=f"conversation/session-s1/turn-{spans[0][0]}",
            event_time=_iso(NOW), ingest_time=_iso(NOW),
            turn_refs=[f"turn-{t}" for s, e in spans
                       for t in range(s, e + 1)],
            summary="摘要", core="叙述",
        ))
        self.svc.turn_spans.fill(eid, spans, filled_by="test")

    def test_single_turn_expands_plus_minus_three(self) -> None:
        self._add("EVT-A", [(5, 5)])
        out = self.svc.read_source_event("EVT-A")
        self.assertEqual(out["status"], "ok")
        self.assertTrue(out["context_expanded"])
        self.assertEqual(self.captured[-1], {2, 3, 4, 5, 6, 7, 8})
        self.assertIn("原文5", out["transcript"])

    def test_real_interval_kept_as_is(self) -> None:
        self._add("EVT-B", [(10, 12)])
        out = self.svc.read_source_event("EVT-B")
        self.assertEqual(out["status"], "ok")
        self.assertFalse(out["context_expanded"])
        self.assertEqual(self.captured[-1], {10, 11, 12})

    def test_head_clamped_when_not_enough_before(self) -> None:
        self._add("EVT-C", [(1, 1)])
        out = self.svc.read_source_event("EVT-C")
        self.assertEqual(out["status"], "ok")
        self.assertEqual(self.captured[-1], {1, 2, 3, 4})


if __name__ == "__main__":
    unittest.main(verbosity=2)
