# -*- coding: utf-8 -*-
"""Ember v3 — Cue semantic 匹配层（架构文档 §2.4"匹配方式统一为 semantic"）。

这个文件是干什么的：
  词面召回（DirectRecall）之后，对词面未命中的 active lexical Cue 做
  语义扩展检查——输入向量 vs cue 向量的 cosine 相似度达阈值即命中。
  语义词面同权：semantic 命中与 lexical 命中一样计入 multi_cue 聚合、
  召回记账与收紧（momo 2026-10-09 拍板 A 方案：按条统一管线，
  词面未命中的 cue 全部做语义检查，词面/语义命中可共存）。

被谁调用：
  - ember/recall.py 的 DirectRecall（召回主路径第二段）
  - scripts/build_cue_vectors.py（离线预计算 cue_vectors.jsonl）

向量文件：cue_vectors.jsonl（与 direction_vectors.jsonl 同模式，
  route_id → {cue, vector, model}），source of truth 是存储文件，
  由 build_cue_vectors.py 离线重建；新写的 cue 在重建前没有向量，
  只参与词面匹配，不报错（基础设施缺位不引入新行为）。

铁律：
- CUE_SEMANTIC_FLOOR 是 baseline 占位，不是已证明设计——
  标定方法同 Direction（取数实验），启用前须用真实数据标定；
- embedding API 失败 → 本轮语义段整体降级为纯词面结果
  （reason=embed_failed，与 Direction 的降级同款），绝不阻断召回。
"""
from __future__ import annotations

import json
import os

from typing import Any

from .direction import cosine, embed_text
from .env import load_env

load_env(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CUE_VECTORS_FILE = "cue_vectors.jsonl"

# 初标定（2026-10-15，momo 实测：4 条 cue × 6 个查询词的 cosine 分布）——
# 同义带（喜好 0.891 / 喜欢 0.791 / 热爱 0.763）与无关带（工作 0.606 /
# 测试 0.598 / 天气 0.575）之间空档 0.61~0.76，取 0.70 两边留余量。
# 样本仍小，cue 库变大后值得重标（对照 Direction 聚焦阈值 0.47 是
# 长文本口径，Cue 短语分布不同，不可混用）。
CUE_SEMANTIC_FLOOR = 0.70


class CueVectorStore:
    """cue_vectors.jsonl 的只读加载：route_id → {cue, vector, model}。

    source of truth 是存储文件；由 scripts/build_cue_vectors.py
    从 recall_routes.jsonl 的 active lexical cue 离线重建
    （换 embedding 模型全量重算，新旧模型向量绝不能混比）。
    """

    def __init__(self, root: str):
        self.root = root
        self._vectors: dict[str, dict[str, Any]] = {}
        self._load()

    def _path(self) -> str:
        return os.path.join(self.root, CUE_VECTORS_FILE)

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
                self._vectors[d["route_id"]] = d

    def get(self, route_id: str) -> dict[str, Any] | None:
        return self._vectors.get(route_id)

    def has(self, route_id: str) -> bool:
        return route_id in self._vectors

    def size(self) -> int:
        return len(self._vectors)


def semantic_hits(input_text: str,
                  routes: list,
                  vector_store: CueVectorStore,
                  floor: float = CUE_SEMANTIC_FLOOR,
                  embed_fn=None) -> tuple[list, str]:
    """对词面未命中的 active lexical routes 做语义检查。

    参数：
      input_text   本轮用户输入
      routes       词面未命中的候选 RecallRoute 列表（matcher=lexical、
                   status=active，由 DirectRecall 筛好后传入）
      vector_store CueVectorStore（无向量的 route 跳过——离线重建前
                   只参与词面，合法状态）
      floor        语义命中阈值（baseline 占位，见文件头铁律）
      embed_fn     向量函数（缺省 direction.embed_text；测试注入 mock）

    返回 (命中 route 列表, reason)：
      reason = ok           正常产出（含空命中）
      reason = no_vectors   库里没有任何 cue 向量（未重建过），跳过语义段
      reason = embed_failed embedding 失败，本轮降级纯词面
    命中的 route 原样返回（调用方自行构造 RouteHit，matcher 记 semantic）。
    """
    candidates = [r for r in routes if vector_store.has(r.route_id)]
    if not candidates:
        return [], "no_vectors"
    embed = embed_fn or embed_text
    try:
        q_vec = embed(input_text)
    except Exception:  # noqa: BLE001 — 基础设施失败统一降级，不阻断召回
        return [], "embed_failed"
    hits: list = []
    for r in candidates:
        stored = vector_store.get(r.route_id)
        sim = cosine(q_vec, stored["vector"])
        if sim >= floor:
            hits.append(r)
    return hits, "ok"
