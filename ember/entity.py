# -*- coding: utf-8 -*-
"""Ember v3 — Entity 与 Direction（第 6 认知对象，手动提取第一阶段）。

依据：ember-v3-Entity-Recall-方向文档（momo 2026-10-01/02 定稿 + 10-02 01:31 处置裁决）。

语义归属（momo 10-02 裁决）：
- Direction 是 Entity 的附属结构，不是独立认知对象、不是全局 graph。
- Direction = Entity 上稳定、粗粒度的语义入口；不指出具体事件、
  不携带具体主题、不能替代 Cue。
- Direction Vector = embedding(描述文本)。本阶段无 embedding 模型，
  向量与阈值（0.87/0.63/0.81 仅为示例，baseline）暂缺，只建结构。

铁律：本文所有默认值都是 baseline 假设，不是已证明设计。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any

ENTITIES_FILE = "entities.jsonl"


@dataclass
class Direction:
    """Entity 的附属结构：从哪个语义方向进入这个 Entity 的记忆空间。

    schema 未冻结（momo 10-02 裁决：归属先定，schema 后定）。
    现存字段是手动提取阶段的最小集合。
    """
    direction_id: str
    description: str          # 稳定语义描述文本（将来 Direction Vector = embedding(description)）
    # vector 留空 —— embedding 模型未绑定（momo 10-02 裁决：不提前绑定，换模型重算属迁移问题）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Direction":
        return cls(**d)


@dataclass
class Entity:
    """长期稳定认知锚点（第 6 认知对象）。

    手动提取阶段（momo 2026-10-03 裁决，方案 A）：
    - "Ember" 的三个指称（记忆系统 / 助手 / 测试前端）暂合为一个 Entity，
      别名归并（canonicalization）留给后续 Sleep/挂载层，不现在拆。

    生命周期（架构文档 §2.1，2026-10-08 定稿）：
    - status: open / closed
    - open 期间：其下任何 Event 的 Cue 都不参与 Sleep 收紧（活跃豁免）
    - 收紧起算点 = closed_at（Entity 被 close 的时刻）
    """
    entity_id: str
    name: str                 # 规范名（canonical name）
    aliases: list[str] = field(default_factory=list)   # 已知别名（手动登记，不穷举）
    directions: list[Direction] = field(default_factory=list)  # 附属结构（非全局 graph）
    status: str = "open"      # open / closed
    closed_at: str = ""       # close 时刻（ISO）；open 时为空
    provenance_refs: list[str] = field(default_factory=list)

    ALLOWED_STATUS = ("open", "closed")

    def __post_init__(self):
        if self.status not in Entity.ALLOWED_STATUS:
            raise ValueError(
                f"Entity.status 必须是 {Entity.ALLOWED_STATUS}，得到 {self.status!r}")
        if self.status == "open" and self.closed_at:
            raise ValueError("open 状态的 Entity 不能有 closed_at")

    def close(self, at_iso: str) -> None:
        """关闭实体：进入静置期，其下 Cue 开始可被 Sleep 收紧。"""
        if self.status == "closed":
            raise ValueError(f"Entity {self.entity_id} 已关闭")
        self.status = "closed"
        self.closed_at = at_iso

    def reopen(self) -> None:
        """重开实体：活跃豁免恢复，收紧起算点失效。"""
        self.status = "open"
        self.closed_at = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Entity":
        dirs = [Direction.from_dict(x) for x in d.pop("directions", [])]
        return cls(**d, directions=dirs)


class EntityStore:
    """entities.jsonl 的读写。source of truth 是存储文件；加载即全量，索引可重建。"""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._entities: dict[str, Entity] = {}
        self._load()

    def _path(self) -> str:
        return os.path.join(self.root, ENTITIES_FILE)

    def _load(self) -> None:
        p = self._path()
        if not os.path.exists(p):
            return
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                e = Entity.from_dict(json.loads(line))
                self._entities[e.entity_id] = e

    def save(self) -> None:
        """手动提取阶段用整文件重写（量小）；将来批量/离线挂载再改 append。"""
        with open(self._path(), "w", encoding="utf-8") as f:
            for e in self._entities.values():
                f.write(json.dumps(e.to_dict(), ensure_ascii=False) + "\n")

    def upsert(self, entity: Entity) -> Entity:
        self._entities[entity.entity_id] = entity
        return entity

    def close(self, entity_id: str, at_iso: str) -> Entity:
        """关闭实体并落盘：进入静置期，其下 Cue 可被 Sleep 收紧（起算 closed_at）。"""
        e = self.get(entity_id)
        e.close(at_iso)
        self.save()
        return e

    def reopen(self, entity_id: str) -> Entity:
        """重开实体并落盘：活跃豁免恢复。"""
        e = self.get(entity_id)
        e.reopen()
        self.save()
        return e

    def get(self, entity_id: str) -> Entity:
        return self._entities[entity_id]

    def find_by_name(self, name: str) -> Entity | None:
        name_l = name.strip().lower()
        for e in self._entities.values():
            if e.name.strip().lower() == name_l:
                return e
            if any(a.strip().lower() == name_l for a in e.aliases):
                return e
        return None

    def all(self) -> list[Entity]:
        return list(self._entities.values())
