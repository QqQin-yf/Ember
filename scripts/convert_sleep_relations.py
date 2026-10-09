# -*- coding: utf-8 -*-
"""把 sleep_state.jsonl 的 superseded 判定离线转换为 Event Relation（resolves 边）。

momo 2026-10-05 裁决走 Relation discovery v1：Sleep 的 status_evidence
已经反指修正者，转换是确定性的（不再调 LLM），一次生成落盘。

产出：data_real/event_relations.jsonl（5 条 resolves 边）
幂等：重复运行覆盖重写（relation_id 由 src/dst 决定，稳定）。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember.relation import EventRelation  # noqa: E402

ROOT = os.path.join(BASE, "data_real")
SLEEP = os.path.join(ROOT, "sleep_state.jsonl")
OUT = os.path.join(ROOT, "event_relations.jsonl")

relations = []
with open(SLEEP, encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if d.get("status") != "superseded":
            continue
        src = d["event_id"]
        ev = d.get("status_evidence") or ""
        # 从 status_evidence 里提取修正者 id（EVT-XXX-xxxxxx 或 EVT-XXX 前缀）
        import re
        m = re.search(r"EVT-\d{3}(?:-[0-9a-f]{6})?", ev)
        if not m:
            print(f"!! {src[:7]}: status_evidence 中未找到修正者 id，跳过")
            continue
        dst = m.group(0)
        # glm 偶尔只写短前缀（如 EVT-013），补全成完整 event_id（查 events.jsonl）
        if "-" not in dst[4:] and not dst.count("-") >= 2:
            with open(os.path.join(ROOT, "events.jsonl"), encoding="utf-8") as ef:
                for el in ef:
                    ed = json.loads(el)
                    if ed["event_id"].startswith(dst):
                        dst = ed["event_id"]
                        break
        rid = f"rel_{src.split('-')[1]}_resolves_{dst.split('-')[1]}"
        relations.append(EventRelation(
            relation_id=rid,
            src_event=src,
            dst_event=dst,
            type="resolves",
            confidence="high",
            status="active",
            evidence_refs=[f"sleep-v1:status_evidence({src[:7]})"],
            provenance_refs=["sleep-v1", "relation-converter"],
        ))

with open(OUT, "w", encoding="utf-8") as f:
    for r in relations:
        f.write(json.dumps(r.to_dict(), ensure_ascii=False) + "\n")

print(f"写入 {len(relations)} 条 resolves 边到 {OUT}")
for r in relations:
    print(f"  {r.relation_id}: {r.src_event[:7]} --resolves--> {r.dst_event[:7]}")
