# -*- coding: utf-8 -*-
"""Ember v3 — Dream 账本（heart 梦通道，旁路层）。

对齐背景（heart 梦架构 v3，2026-10-10 契约定稿）：
- 一次性批量推送：Ember 选材 → POST /api/dream/material → 收 dream_id 即收工。
- "偶尔混旧"进 heart 的哲学留档（"梦不排道理"）：辅料 = 联想渗漏 + 无来由旧事。

职责（本账本两件事）：
1. 已入梦标记：夹旧料时优先挑没梦过的旧 Event（避免夜夜梦同一件事）；
2. 夜留痕：每夜推了哪些 event_id、heart 的 dream_id、跳过原因——
   事后可回溯"那晚的梦来自哪些经历"。

铁律：
- 旁路文件（dream_ledger.jsonl），绝不写进 Event 本体。
- 账本缺失不阻断主流程：无账本 = 全部事件视为未入梦。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

DREAM_LEDGER_FILE = "dream_ledger.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class DreamLedger:
    """dream_ledger.jsonl 的追加写与查询。source of truth 是存储文件。"""

    def __init__(self, root: str):
        self.root = root
        # event_id -> 已入梦次数（按文件实时统计，文件是唯一事实源）
        self._count_cache: dict[str, int] | None = None

    def _path(self) -> str:
        return os.path.join(self.root, DREAM_LEDGER_FILE)

    def _load_counts(self) -> dict[str, int]:
        if self._count_cache is not None:
            return self._count_cache
        counts: dict[str, int] = {}
        p = self._path()
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("kind") != "night" or d.get("status") != "ok":
                        continue  # 只统计成功夜（failed=材料没进殿堂，可再挑）
                    for eid in d.get("event_ids", []):
                        counts[eid] = counts.get(eid, 0) + 1
        self._count_cache = counts
        return counts

    def dreamed_of(self, event_id: str) -> bool:
        """该 Event 是否入过梦（任何一次成功夜）。"""
        return event_id in self._load_counts()

    def dream_count(self, event_id: str) -> int:
        return self._load_counts().get(event_id, 0)

    def record_night(self, event_ids: list[str], dream_id: str | None,
                     skipped: list[dict[str, str]] | None = None,
                     status: str = "ok") -> dict[str, Any]:
        """记一夜：推了什么、heart 收没收、跳过了什么。

        status="ok" 计入已入梦统计（dream_id 可为 null = 无梦之夜，
        空推也记，联调与"这晚没梦"的回溯都需要）；
        status="failed" 只留痕不计入统计（材料没进殿堂，旧事仍可再挑）。
        """
        entry = {
            "kind": "night",
            "status": status,
            "event_ids": list(event_ids),
            "dream_id": dream_id,
            "skipped": list(skipped or []),
            "recorded_at": _now_iso(),
        }
        with open(self._path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._count_cache = None  # 失效缓存，下次重读
        return entry

    def nights(self) -> list[dict[str, Any]]:
        """全部夜记录（按文件顺序，观察用）。"""
        p = self._path()
        if not os.path.exists(p):
            return []
        out = []
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out

    def to_dict(self) -> dict[str, Any]:
        ns = self.nights()
        return {
            "nights": len(ns),
            "ok_nights": sum(1 for n in ns if n.get("status") == "ok"),
            "events_dreamed": len(self._load_counts()),
        }
