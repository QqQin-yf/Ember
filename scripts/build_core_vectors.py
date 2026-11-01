# -*- coding: utf-8 -*-
"""Ember v3 — 离线重建内容语义向量（core_vectors.jsonl + entity_vectors.jsonl）。

被谁调用：手工离线运行（python scripts/build_core_vectors.py）。
运行时机：首批建索引、新事件攒量后增量、换 embedding 模型后全量重算
（夜间 structure/run 收尾也会对新事件/新 summary 增量补算——
本脚本是全量兜底与换模型入口）。

职责：
  - 全部事件的 core（含 superseded——被推翻的也要捞得到）→ 向量，
    写 core_vectors.jsonl（event_id → {core, vector, model}，整文件重写）；
  - 非空实体 summary → 向量，写 entity_vectors.jsonl（entity_id → …）；
    summary 为空的实体跳过（如实：还没夜批过的实体不出卡）。

铁律：换 embedding 模型 = 向量空间迁移，新旧向量绝不能混比——
换模型后必须全量重算两个文件。
"""
from __future__ import annotations

import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember.recall_embedding import embedding_model, embed_text  # noqa: E402
from ember.env import load_env  # noqa: E402
from ember.store import EmberStore  # noqa: E402
from ember.entity import EntityStore  # noqa: E402

DATA = os.environ.get("EMBER_DATA_DIR", os.path.join(BASE, "data"))

load_env(BASE)


def _write(path: str, rows: list[dict], key: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"  {os.path.basename(path)} 重写完成：{len(rows)} 条"
          f"（model={rows[0]['model'] if rows else '-'}）", file=sys.stderr)


def main() -> None:
    store = EmberStore(DATA)
    estore = EntityStore(DATA)
    model = embedding_model()

    # ---- 事件 core（含 superseded）----
    events = store.all_events()
    core_rows: list[dict] = []
    for i, ev in enumerate(events, 1):
        text = (ev.core or ev.summary or "").strip()
        if not text:
            continue
        core_rows.append({"event_id": ev.event_id, "text": text,
                          "vector": embed_text(text), "model": model})
        print(f"  [{i}/{len(events)}] {ev.event_id} dim="
              f"{len(core_rows[-1]['vector'])}", file=sys.stderr)
    _write(os.path.join(DATA, "core_vectors.jsonl"), core_rows, "event_id")

    # ---- 实体 summary（非空才算）----
    ent_rows: list[dict] = []
    ents = [e for e in estore.all() if (e.summary or "").strip()]
    for i, e in enumerate(ents, 1):
        text = e.summary.strip()
        ent_rows.append({"entity_id": e.entity_id, "text": text,
                         "vector": embed_text(text), "model": model})
        print(f"  [{i}/{len(ents)}] entity {e.name} dim="
              f"{len(ent_rows[-1]['vector'])}", file=sys.stderr)
    _write(os.path.join(DATA, "entity_vectors.jsonl"), ent_rows, "entity_id")


if __name__ == "__main__":
    main()
