# -*- coding: utf-8 -*-
"""Ember v3 — 离线预计算 cue_vectors.jsonl（Cue 语义匹配的向量文件）。

被谁调用：手工离线运行（python scripts/build_cue_vectors.py）。
运行时机：写入链新增 cue 后、或换 embedding 模型后全量重算。
（新 cue 在重建前没有向量，运行时只参与词面匹配，不报错。）

职责：
  读 {EMBER_DATA_DIR}/recall_routes.jsonl 中 status=active 且 matcher=lexical
  的全部 route（去重：同一 cue 文本只算一次向量），逐条调
  ember.direction.embed_text（硅基流动 Qwen3-Embedding-8B）产出向量，
  整文件重写 cue_vectors.jsonl（route_id → {cue, vector, model}）。

不做：任何运行时判断；retired/非 lexical route 的向量（它们不参与召回）。
"""
from __future__ import annotations

import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember.direction import embed_text  # noqa: E402
from ember.env import load_env  # noqa: E402

DATA = os.environ.get("EMBER_DATA_DIR", os.path.join(BASE, "data"))
ROUTES_FILE = os.path.join(DATA, "recall_routes.jsonl")
OUT_FILE = os.path.join(DATA, "cue_vectors.jsonl")

load_env(BASE)


def main() -> None:
    routes: dict[str, dict] = {}      # route_id → route 行
    for line in open(ROUTES_FILE, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        # 同一 route_id 多行时后行覆盖（查询端后行覆盖的既有口径）
        routes[d["route_id"]] = d

    # active + lexical 才参与召回；同文本 cue 复用同一个向量
    targets = [r for r in routes.values()
               if r.get("status") == "active" and r.get("matcher") == "lexical"]
    by_cue: dict[str, str] = {}       # cue 小写 → 首个 route_id（代表）
    reps: list[tuple[str, str]] = []  # (代表 route_id, cue 原文)
    for r in sorted(targets, key=lambda x: x["route_id"]):
        key = r["cue"].strip().lower()
        if not key:
            continue
        if key not in by_cue:
            by_cue[key] = r["route_id"]
            reps.append((r["route_id"], r["cue"]))

    print(f"active lexical routes {len(targets)} 条，"
          f"去重后待向量化 {len(reps)} 条 ...", file=sys.stderr)

    model_used: str | None = None
    rows: list[dict] = []
    for i, (rid, cue) in enumerate(reps, 1):
        vec = embed_text(cue)
        # model 取自环境变量回显（与 build_direction_vectors 同口径）
        model_used = os.environ.get(
            "EMBER_EMBED_MODEL", "Qwen/Qwen3-Embedding-8B")
        rows.append({"route_id": rid, "cue": cue,
                     "vector": vec, "model": model_used})
        print(f"  [{i}/{len(reps)}] {cue[:24]}  dim={len(vec)}",
              file=sys.stderr)

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"cue_vectors.jsonl 重写完成：{len(rows)} 条"
          f"（model={model_used}）", file=sys.stderr)


if __name__ == "__main__":
    main()
