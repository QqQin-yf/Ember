# -*- coding: utf-8 -*-
"""Entity 最小闭环验收测试（momo 2026-10-03 裁决范围）。

只验证：当一句话明确指向 Entity（Ember）时，能否进入该 Entity
负责的整个区域（12 条 Event 全部带出，不按 Direction 筛）。
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.store import EmberStore
from ember.entity import EntityStore
from ember.recall import DirectRecall, EntityRecall, register_event_entity_refs

REAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_real")

_ok = 0
_fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  [PASS] {name}")
    else:
        _fail += 1
        print(f"  [FAIL] {name}  {detail}")


def main() -> None:
    print("T-Entity: 最小闭环（明确指向 Entity → 整个区域）")

    store = EmberStore(REAL)
    estore = EntityStore(REAL)
    register_event_entity_refs(os.path.join(REAL, "events.jsonl"))
    er = EntityRecall(store, estore)

    # 1. Entity 加载
    ent = estore.find_by_name("Ember")
    check("Entity Ember 已登记", ent is not None)
    check("别名含 'Ember 记忆系统'", ent and "Ember 记忆系统" in ent.aliases, str(ent and ent.aliases))
    check("4 个 Direction", ent and len(ent.directions) == 4)

    # 2. 明确指向：整句包含实体名
    hits, eids = er.recall("Ember 最近在忙什么？")
    check("「Ember 最近在忙什么」命中 Entity", len(hits) == 1)
    check("带出整个区域 = 12 条", len(eids) == 12, f"got {len(eids)}")

    # 3. 别名命中
    hits2, eids2 = er.recall("你那个 Ember 记忆系统是怎么记东西的？")
    check("别名句命中 Entity", len(hits2) == 1 and hits2[0].matched_via in ("Ember", "Ember 记忆系统"),
          str([(h.name, h.matched_via) for h in hits2]))
    check("别名路径同样带出 12 条", len(eids2) == 12, f"got {len(eids2)}")

    # 3.5 通用词 'ai' 不应触发（2026-10-03 momo 裁决：ai 不是 Ember 的别名）
    hits3, _ = er.recall("你是怎么记东西的，ai？")
    check("通用词 'ai' 不触发 Entity", len(hits3) == 0, f"got {hits3}")

    # 4. 无明确指向：不触发（河南话题）
    hits3, eids3 = er.recall("国庆去河南好玩吗")
    check("河南话题不触发 Ember 区域", len(hits3) == 0 and len(eids3) == 0)

    # 5. Cue 通路仍然独立工作（对照）
    ctx = DirectRecall(store).recall("堂姐结婚了吗")
    check("Cue 通路照常命中婚礼记忆", len(ctx.recalled_events) >= 1)

    # 6. 双路合流（service 级）：内存 store 模拟
    tmp = tempfile.mkdtemp(prefix="ember_ent_test_")
    s2 = EmberStore(tmp)
    e2 = EntityStore(tmp)
    from ember.entity import Entity, Direction
    e2.upsert(Entity(
        entity_id="ent_x", name="测试实体", aliases=[],
        directions=[Direction(direction_id="d1", description="x")]))
    e2.save()
    from ember.models import Event
    for i in range(3):
        s2.write_event(Event(
            event_id=f"evt_{i}", version_id=f"evt_{i}-v1",
            source_ref="s", raw_ref=f"s/t{i}", event_time="now", ingest_time="now",
            summary=f"测试事件{i}"))
    # 内存 store 无 JSONL 挂载信息 → 直接打内存映射
    from ember import recall as R
    R._EVENT_ENTITY_REFS.clear()
    for i in range(3):
        R._EVENT_ENTITY_REFS[f"evt_{i}"] = ["ent_x"]
    hits4, eids4 = EntityRecall(s2, e2).recall("测试实体怎么样")
    check("内存 store 合流路径", len(hits4) == 1 and len(eids4) == 3,
          f"hits={len(hits4)} eids={len(eids4)}")

    print(f"\n{_ok} passed, {_fail} failed")
    sys.exit(1 if _fail else 0)


if __name__ == "__main__":
    main()
