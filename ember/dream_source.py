# -*- coding: utf-8 -*-
"""Ember v3 — 梦选材器（heart 梦通道 v3，片段源）。

对齐背景（heart 梦架构 v3，2026-10-10 契约定稿）：
- 材料 = {events: [{text, turn_span}]}，一次性批量推送。
- turn_span 必填：heart 侧验存在性（指向 session.jsonl 没有过的轮次
  直接 400 拒收）——换不出 heart turn_id 区间的事件跳过不推。
- text 用 Event.core 原文（momo 2026-10-10 定）。
- 红线：面向 Claude 的文本不出现"身体"，一律 "heart"——
  违规片段跳过并记录，不做机械替换（改坏语义比不推更糟）。

选材口径（momo 2026-10-10 采纳的巧思，heart 哲学留档"梦不排道理"）：
- 主料：从未入梦的全部 Event（新写的自然在内——首夜即全部）。
- 辅料（最多 2 条，有梦味地挑）：
  a. 联想渗漏：从主料沿 Relation 边（任意方向、active）走一跳，
     把"和今晚的事相连的旧事"带进梦——日有所思夜有所梦；
  b. 无来由的梦：从未入梦的旧 Event 里随机抽 1 条——
     偶尔让多年前的场景冒出来，不解释。
- 总量上限 MAX_EVENTS（默认 8，可调），超出按 主料→渗漏→随机 截断。

不做的：salience（v3 已废除该字段）；任何排序——推给 heart 的顺序
就是"主料在先、辅料在后"的自然顺序，heart 无排序无过滤。
"""
from __future__ import annotations

import random
import re
from typing import Any

MAX_EVENTS = 8          # 单夜总量上限（momo 拍板前的保守 baseline）
MAX_ASSOCIATED = 2      # 辅料 a（联想渗漏）上限

# 红线词检查：面向 Claude 的文本不出现"身体"。
# 匹配"身体"二字（含被标点/空白隔开的写法不做——过宽易误杀，先查连写）。
BANNED_PATTERN = re.compile(r"身体")


def violates_redline(text: str) -> bool:
    return bool(BANNED_PATTERN.search(text or ""))


class DreamSource:
    """从 Ember 整理流选"值得入梦"的事件，组装 heart v3 材料结构。"""

    def __init__(self, store, turn_spans, turn_ledger, dream_ledger,
                 relation_store):
        self.store = store
        self.turn_spans = turn_spans
        self.turn_ledger = turn_ledger
        self.dream_ledger = dream_ledger
        self.relation_store = relation_store

    # ---- ember 区间 → heart turn_id 区间（换不出的部分收缩，全换不出 → None）----

    @staticmethod
    def _session_of(event) -> str | None:
        """Event.source_ref → session_id（"conversation/session-<sid>"）。"""
        ref = getattr(event, "source_ref", "") or ""
        prefix = "conversation/session-"
        if ref.startswith(prefix) and len(ref) > len(prefix):
            return ref[len(prefix):]
        return None

    def heart_span_of(self, event) -> list[str] | None:
        """Event 的 heart turn_id 区间 [起始, 结束]（heart v3 契约：两元素）。

        ember 侧区间可能断开（spans 列表）；heart 只要一段——取全部
        能换出 heart id 的轮次的 [min, max]。全换不出 → None（跳过不推）。
        """
        sid = self._session_of(event)
        if not sid:
            return None
        spans = self.turn_spans.span_of_event(event)
        turns: list[int] = []
        for s, e in spans:
            turns.extend(range(s, e + 1))
        heart_ids: list[str] = []
        for t in turns:
            hid = self.turn_ledger.heart_id_of(sid, t)
            if hid:
                heart_ids.append(hid)
        if not heart_ids:
            return None
        seqs = []
        for hid in heart_ids:
            try:
                seqs.append(int(str(hid).lstrip("t-")))
            except ValueError:
                continue
        if not seqs:
            return None
        return [f"t-{min(seqs):06d}", f"t-{max(seqs):06d}"]

    # ---- 选材 ----

    def _undreamed(self) -> list:
        """从未入梦的全部 Event（稳定排序：event_id）。"""
        return sorted((e for e in self.store.all_events()
                       if not self.dream_ledger.dreamed_of(e.event_id)),
                      key=lambda e: e.event_id)

    def _associated_of(self, main_events: list) -> list:
        """辅料 a：主料沿 Relation 边走一跳（任意方向、active）。

        已入梦的邻居也在池内——"日有所思夜有所梦"，和今晚的事相连的
        旧事值得再次入梦；但优先未入梦的（避免夜夜梦同一件事）。
        主料已含的跳过（不重复推）。
        """
        main_ids = {e.event_id for e in main_events}
        seen: set[str] = set()
        found: list = []
        for ev in main_events:
            for rel in self.relation_store.all():
                if rel.status != "active":
                    continue
                # 任意方向的边都算"相连"——梦不管谁指向谁
                neighbor_id = None
                if rel.src_event == ev.event_id:
                    neighbor_id = rel.dst_event
                elif rel.dst_event == ev.event_id:
                    neighbor_id = rel.src_event
                if not neighbor_id or neighbor_id in main_ids or neighbor_id in seen:
                    continue
                try:
                    neighbor = self.store.get_event(neighbor_id)
                except KeyError:
                    continue
                seen.add(neighbor_id)
                found.append(neighbor)
        # 未入梦优先，同组按 event_id 稳定排序
        found.sort(key=lambda e: (self.dream_ledger.dreamed_of(e.event_id),
                                  e.event_id))
        return found

    def select(self, rng: random.Random | None = None) -> dict[str, Any]:
        """选一夜的材料。返回：
        - events: [{text, turn_span}]（可直接作请求体的 events 字段）
        - pushed_ids / skipped（跳过原因：no_span / redline）
        - associated_ids / random_id（辅料来源留痕）
        """
        rng = rng or random.Random()
        skipped: list[dict[str, str]] = []

        main = self._undreamed()
        # 主料封顶：总量留出辅料名额
        main_cap = MAX_EVENTS - MAX_ASSOCIATED
        main_overflow, main = main[main_cap:], main[:main_cap]

        associated = self._associated_of(main)[:MAX_ASSOCIATED]

        # 辅料 b：无来由的梦——未入梦旧事（主料+渗漏之外）随机 1 条
        used = {e.event_id for e in main} | {e.event_id for e in associated}
        pool = [e for e in main_overflow if e.event_id not in used]
        random_pick = rng.choice(pool) if pool else None
        # 主料被截断时，随机旧事从截断余量里抽——它本来就是"没入梦的旧事"

        chosen = list(main) + associated + ([random_pick] if random_pick else [])
        events: list[dict] = []
        pushed_ids: list[str] = []
        for ev in chosen:
            if violates_redline(ev.core or ""):
                skipped.append({"event_id": ev.event_id, "reason": "redline"})
                continue
            span = self.heart_span_of(ev)
            if span is None:
                skipped.append({"event_id": ev.event_id, "reason": "no_span"})
                continue
            events.append({"text": ev.core, "turn_span": span})
            pushed_ids.append(ev.event_id)

        return {
            "events": events,
            "pushed_ids": pushed_ids,
            "associated_ids": [e.event_id for e in associated],
            "random_id": random_pick.event_id if random_pick else None,
            "skipped": skipped,
        }
