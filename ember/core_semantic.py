# -*- coding: utf-8 -*-
"""Ember v3 — 内容语义向量（三索引之二三：core 向量 + 实体 summary 向量）。

对齐（2026-10-15 设计夜）：search 捞出的地基——
  - core 向量：全事件（含 superseded——被推翻的也要捞得到）按 event_id
    存 core 的向量。cue 收紧剪掉的是「自动浮现」的入口，content 向量
    是「主动检索」的入口，二者正交；
  - entity summary 向量：实体卡（search 命中先出印象层）的匹配依据。
    summary 为空的实体跳过（如实：还没夜批过的实体不出卡）。

现行索引由memory_maintenance后台按内容及向量空间刷新；主动检索按模型/空间匹配查询向量。旧索引及手动构建工具仍可读取，新旧空间不能混比。
"""
from __future__ import annotations

import json
import os
from typing import Any

CORE_VECTORS_FILE = "core_vectors.jsonl"
ENTITY_VECTORS_FILE = "entity_vectors.jsonl"

# search 语义命中下限（baseline 占位：旧版 memory_recall 同值 0.30，
# 原型期观察值——core 是长文本，短查询余弦偏低，标定后调）
SEARCH_HIT_FLOOR = 0.30


class _JsonlVectorStore:
    """向量 jsonl 的读写基类：key → {key 字段, text, vector, model}。

    只读加载 + upsert（增量补算用）：upsert 追加行，加载时后行覆盖
    （与 routes/账本的既有口径一致）。
    """

    KEY_FIELD = "id"
    FILE_NAME = ""

    def __init__(self, root: str):
        self.root = root
        self._vectors: dict[str, dict[str, Any]] = {}
        self._load()

    def _path(self) -> str:
        return os.path.join(self.root, self.FILE_NAME)

    def _load(self) -> None:
        p = self._path()
        if not os.path.exists(p):
            return
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                k = d.get(self.KEY_FIELD)
                if k:
                    self._vectors[k] = d

    def get(self, key: str) -> dict[str, Any] | None:
        return self._vectors.get(key)

    def has(self, key: str) -> bool:
        return key in self._vectors

    def size(self) -> int:
        return len(self._vectors)

    def all(self) -> list[dict[str, Any]]:
        return list(self._vectors.values())

    def upsert(self, key: str, text: str, vector: list[float],
               model: str) -> None:
        """增量补算入口：追加一行（load 时后行覆盖）。"""
        entry = {self.KEY_FIELD: key, "text": text,
                 "vector": vector, "model": model}
        with open(self._path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._vectors[key] = entry


class CoreVectorStore(_JsonlVectorStore):
    """core_vectors.jsonl：event_id → core 向量（含 superseded）。"""

    KEY_FIELD = "event_id"
    FILE_NAME = CORE_VECTORS_FILE


class EntitySummaryVectorStore(_JsonlVectorStore):
    """entity_vectors.jsonl：entity_id → summary 向量（实体卡匹配）。"""

    KEY_FIELD = "entity_id"
    FILE_NAME = ENTITY_VECTORS_FILE


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
