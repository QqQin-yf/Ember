# -*- coding: utf-8 -*-
"""ember 假数据脚本（2026-10-14 momo 拍板：本地只留代码不留数据）。

跑法：python scripts/make_fake_data.py
      （可选环境变量 EMBER_DATA_DIR 覆盖目标目录，默认 data/）
作用：捏造一套形态齐全的假记忆库——事件、召回线索、出处区间、
账本映射、睡眠进度（含冷静期边界演示）、入梦记录。与任何真实
数据零关系；想重来删掉目录重跑即可。

形态清单：
- 2 个会话 × 6 轮（turn_ledger 无条件账 + heart 坐标 + message_id 映射）
- 6 个事件（每事件 2 条 lexical 召回线索 + turn_span 出处区间）
- sleep_progress：前 3 个事件标记已维护（revise 冷静期边界：
  前 3 个改不了走 resolves，后 3 个冷静期内可直改）
- dream_ledger：1 个 ok 夜（前 2 个事件入梦，演示「入梦不再选中」）
- 不造向量（向量是 sleep 派生物，lexical 召回不需要）

安全：目标目录已有 events.jsonl 时拒绝执行（防误伤真实记忆库）。
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.environ.get("EMBER_DATA_DIR", os.path.join(ROOT, "data"))

if os.path.exists(os.path.join(DATA, "events.jsonl")):
    print(f"[seed] 拒绝执行：{DATA} 已有 events.jsonl（可能是真实记忆库）。"
          "要重来请先清空该目录。")
    sys.exit(1)

os.makedirs(DATA, exist_ok=True)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _append(fname: str, row: dict) -> None:
    with open(os.path.join(DATA, fname), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- 轮次账本
# 2 会话 × 6 轮；message_id 用 1000+ 区间（与后端自增 id 形态一致）
SESSIONS = {"s-weekend": (1, 100), "s-cat": (7, 200)}
event_no = 0
for sid, (turn0, mid0) in SESSIONS.items():
    for i in range(6):
        turn = turn0 + i
        _append("turn_ledger.jsonl", {
            "ember_session": sid, "ember_turn": turn,
            "heart_turn_id": f"t-{turn:06d}",
            "user_message_id": mid0 + i * 2,
            "assistant_message_id": mid0 + i * 2 + 1,
            "recorded_at": _now(),
        })

# ---------------------------------------------------------------- 事件
EVENTS = [
    ("EVT-001-aaaaaa", "s-weekend", 1, "我们决定周末去湖边野餐，定在周六上午十点。",
     ["湖边", "周末计划"]),
    ("EVT-002-bbbbbb", "s-weekend", 2, "野餐清单里有三明治、水果，还有一本没看完的书。",
     ["野餐清单"]),
    ("EVT-003-cccccc", "s-weekend", 4, "她说想带上次提到的那本书，我记住了。",
     ["那本书"]),
    ("EVT-004-dddddd", "s-cat", 1, "她的猫第三次把水杯扒拉下桌子，还回瞪她。",
     ["猫", "水杯"]),
    ("EVT-005-eeeeee", "s-cat", 3, "对峙以人类认输告终，猫获得了罐头。",
     ["罐头", "认输"]),
    ("EVT-006-ffffff", "s-cat", 5, "她总结：战场上拿不回来的，谈判桌上拿回来。",
     ["谈判"]),
]

for eid, sid, turn, core, cues in EVENTS:
    event_no += 1
    now = _now()
    event = {
        "event_id": eid,
        "version_id": f"{eid}-v1",
        "source_ref": f"conversation/session-{sid}",
        "raw_ref": f"conversation/session-{sid}/turn-{turn}",
        "event_time": now,
        "ingest_time": now,
        "turn_refs": [f"turn-{turn}"],
        "summary": core[:12],
        "participants": ["ai", "用户"],
        "objects": [],
        "context": [f"会话 {sid} 第 {turn} 轮"],
        "core": core,
        "outcome": ["已约定/已发生"],
        "state": ["平静"],
        "affect": ["轻松"],
        "significance": "假数据：演示召回与修正链路",
        "change": "",
        "details": [f"假事件细节 {event_no}"],
        "key_quotes": [
            {"speaker": "user", "text": f"（假原话 {event_no}）这一句可以在原文里找到。"},
        ],
        "provenance_refs": ["memory-writer", f"chat:{sid}"],
    }
    _append("events.jsonl", event)
    for j, cue in enumerate(cues, start=1):
        _append("recall_routes.jsonl", {
            "route_id": f"rt_{eid}_{j:02d}", "event_id": eid,
            "matcher": "lexical", "cue_type": "lexical", "cue": cue,
            "status": "active",
            "evidence_refs": [f"conversation/session-{sid}/turn-{turn}"],
            "provenance_refs": ["memory-writer"],
        })
    _append("turn_spans.jsonl", {
        "event_id": eid, "spans": [[turn, turn]],
        "filled_by": "seed", "recorded_at": _now(),
    })

# ---------------------------------------------------------------- 睡眠进度
# 前 3 个事件标记已维护（过冷静期 → revise 会拒绝并指路 resolves）
for eid, _, _, _, _ in EVENTS[:3]:
    _append("sleep_progress.jsonl", {
        "event_id": eid, "stage": "judged", "engine": "seed",
        "recorded_at": _now(), "detail": {"note": "seed 预置"},
    })

# ---------------------------------------------------------------- 梦账本
# 一个 ok 夜：前 2 个事件已入梦（DreamSource 不会再选它们）
_append("dream_ledger.jsonl", {
    "kind": "night",
    "event_ids": ["EVT-001-aaaaaa", "EVT-002-bbbbbb"],
    "dream_id": "night-seed0001", "status": "ok", "skipped": [],
    "recorded_at": _now(),
})

print(f"[seed] 完成：假记忆库已造在 {DATA}")
print(f"  事件 6 个（前 3 个已过冷静期 / 后 3 个冷静期内可 revise）")
print(f"  入梦  2 个（EVT-001/002，下夜选材会跳过）")
print(f"  轮次  2 会话 × 6 轮（含 heart 坐标与 message_id 映射）")
print(f"[seed] 配套环境变量（联调时指给对应服务）：")
print(f"  EMBER_SOURCE_URL=<heart-chat 地址>（原文拉取）")
print(f"  HEART_BASE_URL=<heart 地址>（梦推送）")
print(f"[seed] 完全重来：删掉该目录再跑一遍。")
