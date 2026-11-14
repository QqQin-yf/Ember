# -*- coding: utf-8 -*-
"""离线工具：从 entities.jsonl 的方向描述重建 direction_vectors.jsonl。

source of truth 是 entities.jsonl（描述文本）+ 本脚本产出的向量文件
（换 embedding 模型重算即可）。幂等：重复运行覆盖重建。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from ember.recall_embedding import embedding_model, embed_text  # noqa: E402

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DATA = os.environ.get("EMBER_DATA_DIR", os.path.join(BASE, "data"))
ENTITIES = os.path.join(DATA, "entities.jsonl")
OUT = os.path.join(DATA, "direction_vectors.jsonl")


from ember.env import load_env
load_env(BASE)


def main() -> None:
    from ember.maintenance_materials import events
    from ember.privacy import Privacy
    privacy = Privacy(DATA)
    instance = os.environ.get('EMBER_INSTANCE', 'default')
    library = list(events(DATA).values())
    rows = []
    with open(ENTITIES, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            related = [event for event in library if e['entity_id'] in event.get('entity_refs', [])]
            if any(privacy.event_blocked(event, instance) for event in related):
                continue
            sources = [source for event in related for source in privacy.event_sources(event['event_id'], instance)]
            for d in e.get("directions", []):
                vec = embed_text(d["description"], source_root=DATA, sources=sources)
                model = embedding_model()
                rows.append({
                    "direction_id": d["direction_id"],
                    "entity_id": e["entity_id"],
                    "description": d["description"],
                    "model": model,
                    "dim": len(vec),
                    "vector": vec,
                })
                print(f"embedded {d['direction_id']} dim={len(vec)}",
                      file=sys.stderr)
    with open(OUT, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"written {len(rows)} vectors -> {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
