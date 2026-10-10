# -*- coding: utf-8 -*-
"""Ember v3 — Direction 匹配（向量做判断，不做直接召回）。

momo 2026-10-05 通电裁决：
- Entity 明确命中后，对输入做 embedding，与该 Entity 的 Direction
  描述向量比对——向量在这里只做「判断」（选方向 / 判模糊），
  永远不做直接召回裁决：方向下的 Event 由挂载数据（direction_refs）
  决定，不按相似度捞事件。
- 聚焦判据（baseline，取数实验 scripts/exp_direction_match.py，
  **bge-m3 实测**——2026-10-09 换用硅基流动 Qwen3-Embedding-8B 后
  数值不可复用，需重新取数标定，标定前沿用原值仅作占位）：
    top1 >= 0.47 且 margin(top1 - top2) >= 0.08 → 聚焦该方向；
    否则一律视为「模糊」→ 回退 Entity 层 summary（摘要+id，留主动检索入口）。
  实验依据：明确指向句 top1 ≥ 0.48 且 margin ≥ 0.10；模糊句
  margin < 0.05（绝对分可能很高）；无关句绝对分 < 0.42。
- 基础设施失败（embedding 不可用 / 方向向量缺失）→ 不引入新行为，
  回退为 Entity 全区域（通电前的行为）。
- 铁律：阈值是 baseline 假设，不是已证明设计。
"""
from __future__ import annotations

import json
import math
import os

from dataclasses import dataclass, field
from typing import Any, Callable

from .entity import EntityStore
from .env import load_env

load_env(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DIRECTION_VECTORS_FILE = "direction_vectors.jsonl"

# baseline 阈值（exp_direction_match_v2.py 取数，Qwen3-Embedding-8B，
# 2026-10-10；不是已证明设计。换模型或方向增多需重新取数）
TOP1_FLOOR = 0.30
MARGIN_FLOOR = 0.04


def embed_text(text: str, timeout: int = 30) -> list[float]:
    """通过硅基流动 API 调 Qwen3-Embedding-8B 产出向量（momo 2026-10-09 选型）。

    - 服务不可用/超时 → 抛异常，调用方降级（DirectionMatchResult
      reason=embed_failed → Entity 全区域回退，不引入新行为）；
    - 换 embedding 模型 = 向量空间迁移：direction_vectors.jsonl 全量重算
      （build_direction_vectors.py），新旧模型的向量绝不能混比（§5.3）。
    """
    import urllib.request
    api_key = os.environ.get("EMBER_EMBED_API_KEY", "")
    base = os.environ.get("EMBER_EMBED_BASE_URL",
                          "https://api.siliconflow.cn/v1")
    model = os.environ.get("EMBER_EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")
    if not api_key:
        raise RuntimeError("EMBER_EMBED_API_KEY 未配置，无法调用 embedding 服务")
    body = json.dumps({"model": model, "input": text}).encode("utf-8")
    req = urllib.request.Request(
        base.rstrip("/") + "/embeddings", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    vec = data["data"][0]["embedding"]
    if not isinstance(vec, list) or not vec:
        raise ValueError(f"unexpected embedding output: {json.dumps(data)[:120]}")
    return vec


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


class DirectionVectorStore:
    """direction_vectors.jsonl 的只读加载：direction_id → 描述 + 向量。

    source of truth 是存储文件；由 scripts/build_direction_vectors.py
    从 entities.jsonl 的方向描述离线重建（换 embedding 模型重算即可）。
    """

    def __init__(self, root: str):
        self.root = root
        self._vectors: dict[str, dict[str, Any]] = {}
        self._load()

    def _path(self) -> str:
        return os.path.join(self.root, DIRECTION_VECTORS_FILE)

    def _load(self) -> None:
        p = self._path()
        if not os.path.exists(p):
            return
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                self._vectors[d["direction_id"]] = d

    def get(self, direction_id: str) -> dict[str, Any] | None:
        return self._vectors.get(direction_id)

    def has(self, direction_id: str) -> bool:
        return direction_id in self._vectors


@dataclass
class DirectionMatchResult:
    """一次 Direction 匹配的完整判断记录（运行时对象，不落盘）。

    focused=False 时按 reason 区分处置：
    - ambiguous / low_score → Entity 层 summary 回退（设计行为）
    - no_vectors / embed_failed → Entity 全区域回退（基础设施降级）
    """
    focused: bool
    reason: str                      # clear / ambiguous / low_score / no_vectors / embed_failed
    direction_ids: list[str] = field(default_factory=list)  # 本 entity 的全部方向
    sims: dict[str, float] = field(default_factory=dict)    # direction_id → 相似度
    top1: tuple[str, float] | None = None   # (direction_id, sim)
    margin: float | None = None             # top1 - top2

    def to_dict(self) -> dict[str, Any]:
        return {
            "focused": self.focused,
            "reason": self.reason,
            "direction_ids": self.direction_ids,
            "sims": {k: round(v, 4) for k, v in self.sims.items()},
            "top1": list(self.top1) if self.top1 else None,
            "margin": round(self.margin, 4) if self.margin is not None else None,
        }


class DirectionMatcher:
    """输入 → 方向 的向量判断器（只判断，不捞事件）。"""

    def __init__(self, entity_store: EntityStore, vector_store: DirectionVectorStore,
                 embed_fn: Callable[[str], list[float]] | None = None,
                 top1_floor: float = TOP1_FLOOR,
                 margin_floor: float = MARGIN_FLOOR):
        self.entity_store = entity_store
        self.vector_store = vector_store
        self.embed_fn = embed_fn or embed_text
        self.top1_floor = top1_floor
        self.margin_floor = margin_floor

    def match(self, entity_hits: list, query: str) -> DirectionMatchResult:
        """对已命中的 Entity 做方向判断。entity_hits 为空时不应调用本方法。"""
        direction_ids: list[str] = []
        for h in entity_hits:
            try:
                entity = self.entity_store.get(h.entity_id)
            except KeyError:
                continue
            for d in entity.directions:
                if d.direction_id not in direction_ids:
                    direction_ids.append(d.direction_id)
        if not direction_ids:
            return DirectionMatchResult(focused=False, reason="no_vectors",
                                        direction_ids=[])

        with_vecs = [d for d in direction_ids if self.vector_store.has(d)]
        if not with_vecs:
            return DirectionMatchResult(focused=False, reason="no_vectors",
                                        direction_ids=direction_ids)

        try:
            q_vec = self.embed_fn(query)
        except Exception:  # noqa: BLE001 — 基础设施失败统一降级
            return DirectionMatchResult(focused=False, reason="embed_failed",
                                        direction_ids=direction_ids)

        sims = {d: cosine(q_vec, self.vector_store.get(d)["vector"])
                for d in with_vecs}
        ranked = sorted(sims.items(), key=lambda kv: kv[1], reverse=True)
        top1 = ranked[0]
        top2 = ranked[1] if len(ranked) > 1 else (None, 0.0)
        margin = top1[1] - top2[1]

        if top1[1] >= self.top1_floor and margin >= self.margin_floor:
            return DirectionMatchResult(
                focused=True, reason="clear", direction_ids=direction_ids,
                sims=sims, top1=top1, margin=margin)
        reason = "low_score" if top1[1] < self.top1_floor else "ambiguous"
        return DirectionMatchResult(focused=False, reason=reason,
                                    direction_ids=direction_ids,
                                    sims=sims, top1=top1, margin=margin)
