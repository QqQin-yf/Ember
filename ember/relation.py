# -*- coding: utf-8 -*-
"""Ember v3 — Event Relation 持久化层与最小召回（架构文档 §4）。

语义（架构文档 §4）：两个独立 Event 之间有什么联系。它不是 activation 边。

v1 最小实现（momo 2026-10-05 裁决走 Relation discovery）：
- 数据来源：sleep_v1 的 superseded 判定（status_evidence 反指修正者），
  离线转换为 resolves 边——被修正者 → 修正者。
- 运行时行为：一轮召回中某 Event 被 Sleep 过滤排除时，
  沿 resolves 边把修正者（dst）带进本轮召回（"旧观点被什么取代了"的入口）。
- 铁律遵守：
  * confidence 是认知置信度，不参与 runtime gate（§4.1a）——
    gate = relation.status(active) + 触发条件（本轮有 src 被召回意图）。
  * Relation 不存数值，route 字段 v1 留空（route 是 Recall Route 的事，§4.2 分开）。
  * relation 数据可随时重建/删除，不改 Event 本体。

铁律：本层所有默认值是 baseline 假设，不是已证明设计。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any

RELATIONS_FILE = "event_relations.jsonl"

# v1 受控词表（架构文档 §4.1，暂只用 resolves）
RELATION_TYPES = {
    "continues", "concludes", "inspired_by", "causes", "caused_by",
    "responds_to", "resolves", "revisits", "contrasts", "related_to",
}


@dataclass
class EventRelation:
    """有向边：src --type--> dst。"""
    relation_id: str
    src_event: str            # 被修正者（resolves 语义下）
    dst_event: str            # 修正者
    type: str                 # 受控词表（v1 只用 resolves）
    confidence: str = "high"  # 认知置信度标注，不参与 runtime gate
    status: str = "active"
    evidence_refs: list[str] = field(default_factory=list)
    provenance_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EventRelation":
        return cls(
            relation_id=d["relation_id"],
            src_event=d["src_event"],
            dst_event=d["dst_event"],
            type=d["type"],
            confidence=d.get("confidence", "high"),
            status=d.get("status", "active"),
            evidence_refs=list(d.get("evidence_refs") or []),
            provenance_refs=list(d.get("provenance_refs") or []),
        )


class RelationStore:
    """event_relations.jsonl 的加载与查询（工程层，可重建）。"""

    def __init__(self, root: str):
        self.root = root
        self._relations: dict[str, EventRelation] = {}
        p = os.path.join(root, RELATIONS_FILE)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    r = EventRelation.from_dict(json.loads(line))
                    self._relations[r.relation_id] = r

    def all(self) -> list[EventRelation]:
        return list(self._relations.values())

    def add(self, rel: EventRelation) -> EventRelation:
        """落一条边：内存 + append jsonl（工程层追加，可重建）。"""
        self._relations[rel.relation_id] = rel
        p = os.path.join(self.root, RELATIONS_FILE)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rel.to_dict(), ensure_ascii=False) + "\n")
        return rel

    def resolvers_of(self, event_id: str) -> list[EventRelation]:
        """谁修正/解决了这个事件：src(可完整 id 或前缀) -> active resolves 边。"""
        short = "-".join(event_id.split("-")[:2])
        out = []
        for r in self._relations.values():
            if r.type != "resolves" or r.status != "active":
                continue
            if r.src_event == event_id or r.src_event.startswith(short + "-") \
                    or event_id.startswith(r.src_event):
                out.append(r)
        return out
