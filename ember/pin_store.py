# -*- coding: utf-8 -*-
"""Ember v3 — 钉住登记（pin store，旁路层）。

2026-10-17 设计夜定稿（briefing 配套）：
- 钉住 = 用户明确要求长期记住的事，两处联动——
  1. 简报必带（每次截断刷新都进，且给原文层）；
  2. 自动召回不再浮现（既已常驻在场，召回再浮就是复读）；
  search_memory 主动检索不受影响（钉住≠消失）。
- 谁钉：前端 AI 的 pin_memory / unpin_memory 工具（用户说「钉住」时调），
  app 侧 UI 也可走 HTTP 端点。

铁律：登记与 Event 本体分离（同 recall_ledger 哲学）——append-only
jsonl 落盘，Event 行不回写。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

PINNED_FILE = "pinned.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(
        timespec="seconds")


class PinStore:
    """钉住登记：jsonl 行 = {"event_id": ..., "pinned_at": ISO}。"""

    def __init__(self, root: str, on_change=None):
        self.root = root
        self.on_change = on_change
        # event_id -> pinned_at（保序：按钉住时间）
        self._items: dict[str, str] = {}
        self._load()

    def _path(self) -> str:
        return os.path.join(self.root, PINNED_FILE)

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
                eid = d.get("event_id")
                if eid:
                    self._items[eid] = d.get("pinned_at") or ""

    def _save(self) -> None:
        tmp = self._path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for eid, at in self._items.items():
                f.write(json.dumps({"event_id": eid, "pinned_at": at},
                                   ensure_ascii=False) + "\n")
        os.replace(tmp, self._path())

    def add(self, event_id: str) -> bool:
        """钉住（幂等）。返回是否发生了新钉。"""
        eid = (event_id or "").strip()
        if not eid or eid in self._items:
            return False
        self._items[eid] = _now_iso()
        self._save()
        if self.on_change:
            self.on_change(eid)
        return True

    def remove(self, event_id: str) -> bool:
        """取消钉住（幂等）。返回是否发生了取消。"""
        eid = (event_id or "").strip()
        if eid not in self._items:
            return False
        del self._items[eid]
        self._save()
        if self.on_change:
            self.on_change(eid)
        return True

    def is_pinned(self, event_id: str) -> bool:
        return (event_id or "").strip() in self._items

    def all(self) -> list[str]:
        """全部钉住的 event_id（按钉住时间序）。"""
        return list(self._items)

    def to_dict(self) -> dict[str, Any]:
        return {"count": len(self._items), "event_ids": self.all()}
