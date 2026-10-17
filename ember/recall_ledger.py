# -*- coding: utf-8 -*-
"""Ember v3 — 召回记账（recall ledger，旁路层）。

对齐（架构文档 §3.2，2026-10-08 定稿）：
每次召回触发记一行，纯代码落盘。一份记录两处消费：
1. Sleep 调用②候选组装：今晚 Event 的 turn_span 区间 ∩ 记账 turn
   → 找到"白天被召回带出过的事件对"
2. 旧 Cue 收紧判据：Cue 被用到的次数——次数多就留，没用上过就 retire
同时是冷却机制的共用状态来源（冷却待做）。

铁律：
- 记账属于召回层运行时状态，append-only 落盘、可重建，绝不写回 Event 本体。
- 记账不改变召回行为——它只观察，不做 gate。
- relation 展开（修正者顶上）带出的 event 也记 route=relation，
  来源列被顶上的被修正者 id。

文件：recall_ledger.jsonl（append-only）。每行：
{
  "session_id": "...",     # ember 会话
  "turn": N,               # ember 侧轮次序数（触发召回的那轮用户输入）
  "route": "cue|entity|relation",
  "cue": "命中的 cue 文本（route=cue 时）",
  "entity_id": "命中的实体（route=entity 时）",
  "direction": "聚焦的方向 id（聚焦时）",
  "event_ids": [...],      # 本轮该通路带出的 event（去重后）
  "via": "route=relation 时的被修正者 event_id",
  "recorded_at": "ISO 时间"
}
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

LEDGER_FILE = "recall_ledger.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class RecallLedger:
    """召回记账本：append-only jsonl，内存索引可重建。"""

    def __init__(self, root: str):
        self.root = root
        # turn 索引：(session_id, turn) -> [entry]（候选组装按 turn 区间查）
        self.by_turn: dict[tuple[str, int], list[dict[str, Any]]] = {}
        # cue 使用计数：cue 文本 -> 命中次数（观察/展示用）
        self.cue_use_count: dict[str, int] = {}
        # 按 (event_id, cue) 的使用计数——收紧判据的精确口径：
        # 只有 event_ids 里确实含该事件的行，才算这个事件被这个 cue 带出过
        self.cue_use_by_event: dict[tuple[str, str], int] = {}
        # event 被召回计数：event_id -> 次数（观察用）
        self.event_recall_count: dict[str, int] = {}
        # 最后提取时间（提取 = 召回 ∪ 检索，route 不限）——所有遗忘
        # 计时器（收紧闲置/faded 30 天/活跃门 72h）的唯一时钟
        self.last_extracted: dict[str, str] = {}
        # 注入在场（⑧ 在场去重）：(session_id, event_id) -> 最后注入时刻
        # ——记账行即注入事实（名额截断后记账），2h 内已注入不重复全文
        self.last_injected: dict[tuple[str, str], str] = {}
        self._load()

    def _path(self) -> str:
        return os.path.join(self.root, LEDGER_FILE)

    def _load(self) -> None:
        p = self._path()
        if not os.path.exists(p):
            return
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._index(d)

    def _index(self, d: dict[str, Any]) -> None:
        key = (d.get("session_id") or "", d.get("turn"))
        if not isinstance(key[1], int):
            return
        self.by_turn.setdefault(key, []).append(d)
        at = d.get("recorded_at") or ""
        sid = key[0]
        # 被召回计数只数真召回/检索（cue/entity/relation/search/observe）
        # ——briefing 的在场记账不是召回（2026-10-17 修：简报自记曾把
        # 进过简报的事件越刷越高，挤掉真被召回最多的事件占据简报的
        # recalled_top 席位）。last_extracted / last_injected 照旧不限
        # route：提取时钟与在场去重按原设计，简报在场也算一次提取。
        is_recall = d.get("route") != "briefing"
        for eid in d.get("event_ids", []):
            if is_recall:
                self.event_recall_count[eid] = (
                    self.event_recall_count.get(eid, 0) + 1)
            prev = self.last_extracted.get(eid) or ""
            if at > prev:   # ISO 串字典序 = 时间序（同时区）
                self.last_extracted[eid] = at
            ikey = (sid, eid)
            iprev = self.last_injected.get(ikey) or ""
            if at > iprev:
                self.last_injected[ikey] = at
        cue = d.get("cue")
        if d.get("route") == "cue" and cue:
            # 同一轮同一 cue 只算一次使用（多 event 共命中时该 cue 可能出现多行）
            self.cue_use_count[cue] = self.cue_use_count.get(cue, 0) + 1
            for eid in d.get("event_ids", []):
                key = (eid, cue)
                self.cue_use_by_event[key] = self.cue_use_by_event.get(key, 0) + 1

    def record(self, session_id: str, turn: int, route: str,
               event_ids: list[str],
               cue: str | None = None,
               entity_id: str | None = None,
               direction: str | None = None,
               via: str | None = None) -> dict[str, Any]:
        """记一次召回。route ∈ cue / entity / relation。

        event_ids 为空时仍记账——"跑了但没带出"对收紧判据同样有意义
        （cue 被触碰却没命中任何 event，与从未被触碰是两回事）。
        """
        entry = {
            "session_id": session_id,
            "turn": turn,
            "route": route,
            "event_ids": list(dict.fromkeys(event_ids)),  # 去重保序
            "recorded_at": _now_iso(),
        }
        if cue:
            entry["cue"] = cue
        if entity_id:
            entry["entity_id"] = entity_id
        if direction:
            entry["direction"] = direction
        if via:
            entry["via"] = via
        with open(self._path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._index(entry)
        return entry

    def cue_use_count_for_event(self, event_id: str, cue: str) -> int:
        """某事件名下某条 cue 被真正带出过该事件的次数（收紧判据口径）。

        与 cue_use_count（按 cue 文本全局聚合）的区别：同文本 cue 挂在
        多个事件上时，一次使用只计入 event_ids 里实际出现的事件——
        避免时间差导致的"借别人的使用记录保住自己的 cue"。
        """
        return self.cue_use_by_event.get((event_id, cue), 0)

    def turns_of_event(self, event_id: str) -> list[tuple[str, int]]:
        """某 event 曾在哪些 (session, turn) 被召回带出（候选组装用）。"""
        out = []
        for (sid, turn), entries in self.by_turn.items():
            if any(eid == event_id
                   for e in entries for eid in e.get("event_ids", [])):
                out.append((sid, turn))
        return out

    def events_recalled_in_turns(self, turns: list[tuple[str, int]]
                                 ) -> list[str]:
        """给定 (session, turn) 集合，返回这些轮次里被带出的全部 event（候选组装用）。

        session 为 None 时匹配任意 session（sleep 维护攒着的事件时，
        事件写入分布在多个 session，按轮次号跨 session 找）。
        """
        seen: dict[str, None] = {}
        want = {t for _, t in turns} if turns and turns[0][0] is None \
            else set(turns)
        for key, entries in self.by_turn.items():
            if turns and turns[0][0] is None:
                if key[1] not in want:
                    continue
            elif key not in turns:
                continue
            for e in entries:
                for eid in e.get("event_ids", []):
                    seen.setdefault(eid, None)
        return list(seen)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": sum(len(v) for v in self.by_turn.values()),
            "distinct_turns": len(self.by_turn),
            "top_cues": sorted(self.cue_use_count.items(),
                               key=lambda kv: -kv[1])[:10],
            "top_events": sorted(self.event_recall_count.items(),
                                 key=lambda kv: -kv[1])[:10],
        }
