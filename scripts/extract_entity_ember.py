# -*- coding: utf-8 -*-
"""手动提取第一个 Entity：Ember（momo 2026-10-03 裁决，方案 A + 粗粒度 Direction）。

一次性脚本：为 data_real 里 12 条与 Ember 相关的 Event 打 entity_refs，
并登记 Entity 及其 3 个粗粒度 Direction。

方案 A：三个指称（记忆系统 / 助手 / 测试前端）合一，canonical name = "Ember"。
方案 B 拆分被否——15 条的库拆完每片太稀，且 canonicalization 是后续层的活。

铁律：分组归属是手动 baseline 判断，不是已证明设计；错挂由后续 Sleep 修正。
"""
from __future__ import annotations

import json
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.entity import Entity, EntityStore, Direction

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_real")

# ---- 方案 A：一个 Entity，三个 Direction（粗粒度，baseline） ----

ENTITY_ID = "ent_ember"

ENTITY = Entity(
    entity_id=ENTITY_ID,
    name="Ember",
    aliases=["Ember 记忆系统", "Ember 测试前端", "Ember前端", "ai"],
    provenance_refs=["manual-extraction:momo-2026-10-03"],
    directions=[
        Direction(
            direction_id="dir_ember_arch",
            description="用户在问这个实体的机制与设计：架构怎么定、机制怎么改、"
                        "某个设计问题怎么裁决。",
        ),
        Direction(
            direction_id="dir_ember_history",
            description="用户在问这个实体的经历与演变：它经历过什么、出过什么事、"
                        "认知怎么变化。",
        ),
        Direction(
            direction_id="dir_ember_testing",
            description="用户在问这个实体的测试与使用：功能做了什么、效果如何、"
                        "怎么验证。",
        ),
    ],
)

# ---- Event → (Entity, Direction) 手动归属 ----
# ① 系统/架构线 → arch；② 助手经历线 → history；③ 测试前端线 → testing

ASSIGN: dict[str, str] = {
    # ① 记忆系统机制与设计
    "EVT-001-042738": "dir_ember_arch",    # 传播模型 → Route Search
    "EVT-002-c0ef05": "dir_ember_arch",    # Sleep may cue 收紧框架
    "EVT-003-49c6a9": "dir_ember_arch",    # 纯结构信号裁决
    "EVT-012-4c6e6b": "dir_ember_arch",    # 写入策略之辩（分层写入提议）
    "EVT-013-84de42": "dir_ember_arch",    # 写入时机落幕 + 刷新=实验控制
    # ② 助手经历与演变
    "EVT-007-1df1be": "dir_ember_history",  # 发现建造者
    "EVT-008-15098d": "dir_ember_history",  # 断层假设（后被证伪）
    "EVT-009-936f69": "dir_ember_history",  # 澄清：第一代 Ember
    "EVT-010-bc8bdd": "dir_ember_history",  # 第一次召回=假记忆（后被反转）
    "EVT-011-4d272c": "dir_ember_history",  # 反转：真实记忆，跨会话连续性
    # ③ 测试与使用
    "EVT-004-e7e4ed": "dir_ember_testing",  # 模型切换下拉框完成
    "EVT-005-29733b": "dir_ember_testing",  # 同上（汇报+待确认问题）
}

# 不在 ASSIGN 里的 Event（EVT-006/014/015 河南婚礼线）不动。


def main() -> None:
    # 备份
    shutil.copy2(os.path.join(DATA, "events.jsonl"),
                 os.path.join(DATA, "events.jsonl.pre_entity.bak"))

    # 1) 给 Event 打 entity_refs + direction_refs（增量补字段，内容不碰）
    events_path = os.path.join(DATA, "events.jsonl")
    out = []
    tagged = 0
    with open(events_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            eid = e["event_id"]
            if eid in ASSIGN:
                # 幂等：重跑不重复
                refs = e.get("entity_refs", [])
                if ENTITY_ID not in refs:
                    refs.append(ENTITY_ID)
                e["entity_refs"] = refs
                drefs = e.get("direction_refs", [])
                if ASSIGN[eid] not in drefs:
                    drefs.append(ASSIGN[eid])
                e["direction_refs"] = drefs
                tagged += 1
            out.append(json.dumps(e, ensure_ascii=False))
    with open(events_path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")

    # 2) 登记 Entity
    store = EntityStore(DATA)
    store.upsert(ENTITY)
    store.save()

    print(f"tagged {tagged} events with entity_refs/direction_refs")
    print(f"entity '{ENTITY.name}' saved: {len(ENTITY.directions)} directions")
    for d in ENTITY.directions:
        n = sum(1 for v in ASSIGN.values() if v == d.direction_id)
        print(f"  {d.direction_id}: {n} events")


if __name__ == "__main__":
    main()
