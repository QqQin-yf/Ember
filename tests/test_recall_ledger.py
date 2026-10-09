# -*- coding: utf-8 -*-
"""召回记账（recall_ledger）测试。

覆盖（架构文档 §3.2）：
- record 落盘 + 重载索引可重建
- 三段通路各自记账字段正确（cue/entity/relation）
- turns_of_event / events_recalled_in_turns 查询正确（候选组装用）
- cue 使用计数（收紧判据用）
- event_ids 去重保序；空 event_ids 也记账
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.recall_ledger import RecallLedger


def test_record_and_reload():
    with tempfile.TemporaryDirectory() as root:
        led = RecallLedger(root)
        led.record("s1", 3, route="cue", event_ids=["EVT-001"], cue="Ember")
        led.record("s1", 3, route="entity", event_ids=["EVT-002", "EVT-002"],
                   entity_id="ent_ember", direction="dir_ember_mechanism")
        led.record("s1", 4, route="relation", event_ids=["EVT-003"],
                   via="EVT-002")
        led.record("s1", 5, route="entity", event_ids=[], entity_id="ent_x")

        # 重载：索引可从文件重建（by_turn 按 (session, turn) 聚合，4 行记录 3 个键）
        led2 = RecallLedger(root)
        assert sum(len(v) for v in led2.by_turn.values()) == 4
        assert len(led2.by_turn) == 3
        # 去重保序：EVT-002 只出现一次
        e = led2.by_turn[("s1", 3)][1]
        assert e["event_ids"] == ["EVT-002"]
        assert e["entity_id"] == "ent_ember"
        assert e["direction"] == "dir_ember_mechanism"
        # relation 行 via 字段
        r = led2.by_turn[("s1", 4)][0]
        assert r["route"] == "relation" and r["via"] == "EVT-002"
        # 空 event_ids 也记账
        empty = led2.by_turn[("s1", 5)][0]
        assert empty["event_ids"] == []
        print("ok  test_record_and_reload")


def test_cue_use_count():
    with tempfile.TemporaryDirectory() as root:
        led = RecallLedger(root)
        led.record("s1", 1, route="cue", event_ids=["EVT-001"], cue="Ember")
        led.record("s1", 2, route="cue", event_ids=["EVT-001"], cue="Ember")
        led.record("s1", 3, route="cue", event_ids=["EVT-002"], cue="Sleep")
        led.record("s1", 4, route="entity", event_ids=["EVT-001"],
                   entity_id="ent_ember")  # entity 行不计 cue 次数
        led2 = RecallLedger(root)
        assert led2.cue_use_count["Ember"] == 2
        assert led2.cue_use_count["Sleep"] == 1
        assert "Ember" not in {k: v for k, v in led2.cue_use_count.items()
                               if k not in ("Ember", "Sleep")}
        print("ok  test_cue_use_count")


def test_turn_queries():
    with tempfile.TemporaryDirectory() as root:
        led = RecallLedger(root)
        led.record("s1", 1, route="cue", event_ids=["EVT-001"], cue="堂姐")
        led.record("s1", 5, route="entity", event_ids=["EVT-002"],
                   entity_id="ent_ember")
        led.record("s2", 1, route="cue", event_ids=["EVT-003"], cue="Ember")
        led2 = RecallLedger(root)
        # turns_of_event
        assert led2.turns_of_event("EVT-001") == [("s1", 1)]
        assert ("s1", 5) in led2.turns_of_event("EVT-002")
        assert led2.turns_of_event("EVT-999") == []
        # events_recalled_in_turns：跨 session 同 turn 不串
        assert led2.events_recalled_in_turns([("s1", 1), ("s2", 1)]) == \
            ["EVT-001", "EVT-003"]
        assert led2.events_recalled_in_turns([("s1", 5)]) == ["EVT-002"]
        print("ok  test_turn_queries")


def test_file_format():
    with tempfile.TemporaryDirectory() as root:
        led = RecallLedger(root)
        led.record("s1", 1, route="cue", event_ids=["EVT-001"], cue="x")
        p = os.path.join(root, "recall_ledger.jsonl")
        with open(p, encoding="utf-8") as f:
            line = json.loads(f.readline())
        assert {"session_id", "turn", "route", "event_ids",
                "recorded_at"} <= set(line)
        print("ok  test_file_format")


if __name__ == "__main__":
    test_record_and_reload()
    test_cue_use_count()
    test_turn_queries()
    test_file_format()
    print("recall_ledger tests: all passed")
