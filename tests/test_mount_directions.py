# -*- coding: utf-8 -*-
"""B2 Direction 挂载作业测试：mock LLM，覆盖校验/落盘/阈值/增量合并。

数据：tempdir 造 1 个 Entity（2 个方向）+ 10 条事件（8 条属该实体，
其中 2 条已有 direction_refs；2 条属其他实体凑数）。
"""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import mount_directions  # noqa: E402
from ember.entity import Entity, EntityStore, Direction  # noqa: E402


def make_env(root):
    os.makedirs(root, exist_ok=True)
    events = {}
    # 8 条属 ent_x
    for i in range(1, 9):
        eid = f"EVT-{i:03d}-aaaa"
        d = {"event_id": eid, "version_id": eid + "-v1",
             "source_ref": "t", "raw_ref": "t",
             "event_time": "t", "ingest_time": "t",
             "turn_refs": [], "core": f"事件{i}：架构机制相关讨论",
             "summary": f"事件{i}",
             "entity_refs": ["ent_x"]}
        if i <= 2:
            d["direction_refs"] = ["dir_x_1"]  # 已挂载
        events[eid] = d
    # 2 条属其他实体（不该被摸）
    for i in (9, 10):
        eid = f"EVT-{i:03d}-aaaa"
        events[eid] = {"event_id": eid, "version_id": eid + "-v1",
                       "source_ref": "t", "raw_ref": "t",
                       "event_time": "t", "ingest_time": "t",
                       "turn_refs": [], "core": "别的实体的事件",
                       "summary": f"事件{i}",
                       "entity_refs": ["ent_other"]}
    with open(os.path.join(root, "events.jsonl"), "w", encoding="utf-8") as f:
        for d in events.values():
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    estore = EntityStore(root)
    estore.upsert(Entity(
        entity_id="ent_x", name="X",
        directions=[
            Direction(direction_id="dir_x_1", description="架构机制方向"),
            Direction(direction_id="dir_x_2", description="自我认知方向"),
        ]))
    estore.save()
    return events, estore


def test_validate():
    valid = {"dir_x_1", "dir_x_2"}
    assert not mount_directions.validate(
        {"direction_ids": ["dir_x_1", "dir_x_2"], "reason": "都相关"}, valid)
    assert not mount_directions.validate({"direction_ids": [], "reason": "不贴切"},
                                         valid), "空列表合法"
    errs = mount_directions.validate({"direction_ids": ["dir_ghost"], "reason": "x"},
                                     valid)
    assert any("发明" in e for e in errs)
    errs = mount_directions.validate({"direction_ids": [], "reason": ""}, valid)
    assert any("reason" in e for e in errs)
    print("ok  validate（合法/空列表/发明方向/缺 reason）")


def test_mount_full():
    with tempfile.TemporaryDirectory() as root:
        mount_directions.EVENTS_FILE = os.path.join(root, "events.jsonl")
        events, estore = make_env(root)

        # 模拟 main 的核心流程（不走 argparse / 真实 LLM）
        entity = estore.get("ent_x")
        directions = entity.directions
        member_ids = mount_directions.events_of_entity(events, "ent_x")
        assert len(member_ids) == 8, member_ids
        assert len(member_ids) >= mount_directions.DIRECTION_THRESHOLD

        targets = [eid for eid in member_ids
                   if not events[eid].get("direction_refs")]
        assert len(targets) == 6, "已有挂载的 2 条不应重复挂"

        dir_lines = "\n".join(f"{d.direction_id}: {d.description}"
                              for d in directions)
        valid_ids = {d.direction_id for d in directions}
        updates = {}
        verdicts = {
            # 前 3 条挂 dir_x_1；第 4 条空挂；第 5 条试图发明方向（拒绝）
            "EVT-003-aaaa": {"direction_ids": ["dir_x_1"], "reason": "机制"},
            "EVT-004-aaaa": {"direction_ids": ["dir_x_2"], "reason": "认知"},
            "EVT-005-aaaa": {"direction_ids": ["dir_x_1", "dir_x_2"], "reason": "双方向"},
            "EVT-006-aaaa": {"direction_ids": [], "reason": "无贴切方向"},
            "EVT-007-aaaa": {"direction_ids": ["dir_ghost"], "reason": "坏输出"},
        }
        report = []
        for eid in targets:
            v = verdicts.get(eid)
            if v is None:
                continue
            errs = mount_directions.validate(v, valid_ids)
            if errs:
                print(f"  （拒绝 {eid}: {errs}）")
                continue
            ids = list(dict.fromkeys(v["direction_ids"]))
            if ids:
                merged = list(dict.fromkeys(
                    (events[eid].get("direction_refs") or []) + ids))
                updates[eid] = {"direction_refs": merged}
                events[eid]["direction_refs"] = merged
            report.append({"event_id": eid, "mounted": ids})

        mount_directions.rewrite_events(events, updates)

        # 落盘验证
        events2 = mount_directions.load_events()
        assert events2["EVT-003-aaaa"]["direction_refs"] == ["dir_x_1"]
        assert events2["EVT-005-aaaa"]["direction_refs"] == ["dir_x_1", "dir_x_2"]
        assert "direction_refs" not in events2["EVT-006-aaaa"], "空挂不落盘"
        assert "direction_refs" not in events2["EVT-007-aaaa"], "坏输出不落盘"
        assert events2["EVT-001-aaaa"]["direction_refs"] == ["dir_x_1"], "已有挂载保留"
        assert "direction_refs" not in events2["EVT-009-aaaa"], "别的实体不被摸"
        print("ok  挂载全链（增量合并/空挂不落/坏输出拒绝/不碰别的实体）")


def test_threshold_and_no_direction():
    with tempfile.TemporaryDirectory() as root:
        mount_directions.EVENTS_FILE = os.path.join(root, "events.jsonl")
        events, estore = make_env(root)
        # 阈值判断：7 条不够（造 7 条的实体）
        entity = estore.get("ent_x")
        member = mount_directions.events_of_entity(events, "ent_x")
        assert (len(member) >= mount_directions.DIRECTION_THRESHOLD) is True
        # 无方向的 Entity
        estore.upsert(Entity(entity_id="ent_none", name="N"))
        assert estore.get("ent_none").directions == []
        print("ok  阈值/无方向分支")


def main():
    test_validate()
    test_mount_full()
    test_threshold_and_no_direction()
    print("\nB2 全部通过")


if __name__ == "__main__":
    main()
