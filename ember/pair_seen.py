# -*- coding: utf-8 -*-
"""Ember v3 — Sleep pair 留痕（旁路层）。

对齐（架构文档 §4.2，2026-10-08 定稿）：
Sleep 调用②逐对裁决中，判过"无关系"的 pair 落盘留痕，永不再判——
同一段对话每晚都在，没有留痕就会反复消耗裁决调用量。

文件：pair_seen.jsonl（append-only）。每行：
{
  "pair_key": "EVT-001|EVT-008",   # 规范化键（id 排序后拼接，无向）
  "verdict": "none",               # 目前只有 none（无关系）；将来可扩
  "decided_by": "sleep-v2",        # 哪个作业判的
  "recorded_at": "ISO 时间"
}

查询接口 is_seen(pair) 供候选组装过滤；写入只增不改——
裁决结论是"当时判过无"，即使将来事件 version 变了，
历史判断也只作废不删除（由换 version 的作业自己负责重判）。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

PAIR_FILE = "pair_seen.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def pair_key(a: str, b: str) -> str:
    """规范化无向键：两个 event_id 排序后拼接。

    统一用完整 event_id；传短 id（如 EVT-008）也可以，但同一对 pair
    必须始终用同一格式——调用方负责传入 store 里的规范 id。
    """
    x, y = sorted([str(a), str(b)])
    return f"{x}|{y}"


class PairSeenStore:
    """判过"无"的 pair 留痕：append-only，内存集合可重建。"""

    def __init__(self, root: str):
        self.root = root
        self.seen: dict[str, dict[str, Any]] = {}
        self._load()

    def _path(self) -> str:
        return os.path.join(self.root, PAIR_FILE)

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
                k = d.get("pair_key") or ""
                if k:
                    self.seen[k] = d

    def mark(self, a: str, b: str, verdict: str = "none",
             decided_by: str = "sleep-v2") -> dict[str, Any]:
        """标记一个 pair 已判过。重复标记更新内存记录但照常追加留痕。"""
        k = pair_key(a, b)
        entry = {
            "pair_key": k,
            "verdict": verdict,
            "decided_by": decided_by,
            "recorded_at": _now_iso(),
        }
        with open(self._path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.seen[k] = entry
        return entry

    def is_seen(self, a: str, b: str) -> bool:
        """该 pair 是否判过"无"（候选组装过滤用）。"""
        return pair_key(a, b) in self.seen

    def count(self) -> int:
        return len(self.seen)

    def to_dict(self) -> dict[str, Any]:
        return {"seen_pairs": len(self.seen)}
