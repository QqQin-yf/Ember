# -*- coding: utf-8 -*-
"""Ember v3 — Sleep 状态旁路层（读取 sleep_state.jsonl）。

依据（momo 2026-10-04/05 裁决）：
- status=superseded / outdated 的事件**不参与自动召回，但保留在库里**
  （显式检索仍可达，记忆原文不动）。
- 该状态是 Sleep 离线评估的产物，属于旁路文件，绝不写进 Event 本体
  （铁律：Event 不存 activation/brightness/weight 同族）。
- 文件可随时由 scripts/sleep_v1.py 重建；本层只读。

铁律：排除规则（哪些 status 被排除）是 baseline 设计，不是已证明设计。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

SLEEP_STATE_FILE = "sleep_state.jsonl"

# 参与自动召回的 status 白名单（baseline：只有 valid）
RECALL_ALLOWED_STATUSES = {"valid"}
# 被排除的 status（可读性/调试用；与白名单互为补集）
RECALL_EXCLUDED_STATUSES = {"superseded", "outdated"}


@dataclass
class SleepStateStore:
    """只读加载 sleep_state.jsonl，供召回层过滤。

    event_status: event_id -> status（无记录的事件默认 valid，
    即 Sleep 尚未评估过的新事件不会被误伤）。
    """
    root: str
    event_status: dict[str, str] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        p = os.path.join(self.root, SLEEP_STATE_FILE)
        if not os.path.exists(p):
            return
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                eid = d.get("event_id") or ""
                # sleep_v1 落盘的 event_id 可能带完整后缀（EVT-008-15098d），
                # 也可能被只写前缀——统一按"前缀匹配兜底"处理由查询侧完成，
                # 这里同时登记完整 id 与前缀（EVT-008）两种键。
                short = "-".join(eid.split("-")[:2]) if eid else ""
                status = d.get("status") or "valid"
                if eid:
                    self.event_status[eid] = status
                if short and short != eid:
                    self.event_status.setdefault(short, status)
                prov = d.get("_provenance") or "sleep"
                if eid:
                    self.provenance[eid] = prov

    def status_of(self, event_id: str) -> str:
        """查询事件状态；无记录默认 valid（Sleep 未评估过 = 不设防）。"""
        if event_id in self.event_status:
            return self.event_status[event_id]
        short = "-".join(event_id.split("-")[:2])
        return self.event_status.get(short, "valid")

    def recall_allowed(self, event_id: str) -> bool:
        return self.status_of(event_id) in RECALL_ALLOWED_STATUSES

    def excluded_event_ids(self) -> list[str]:
        """当前被排除出自动召回池的事件 id（只返回完整 id 键，短前缀键不算）。"""
        return [eid for eid, s in self.event_status.items()
                if s in RECALL_EXCLUDED_STATUSES and eid.count("-") >= 2]

    def to_dict(self) -> dict[str, Any]:
        return {
            "loaded": bool(self.event_status),
            "excluded_event_ids": self.excluded_event_ids(),
            "statuses": dict(self.event_status),
        }
