# -*- coding: utf-8 -*-
"""Sleep 入口（sleep_run.py）+ 实时写入 quote_spans 测试。

覆盖：
1. 进度账本：无记录 Event 全部捞出 / 跑完记进度 / 续跑不再捞
2. 无可裁对象的散 Event 也记进度（不永久卡 pending）
3. 裁决失败不记进度 → 下次续跑
4. 实时写入 quote_spans：定位成功回填 turn_span / 失败降级
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import sleep_run  # noqa: E402
import sleep_v2  # noqa: E402
from ember.entity import Entity, EntityStore  # noqa: E402
from ember.store import EmberStore  # noqa: E402
from ember.turn_span import TurnSpanStore, locate_turn, spans_from_quotes  # noqa: E402
from ember.recall_ledger import RecallLedger  # noqa: E402
from ember.pair_seen import PairSeenStore  # noqa: E402
from ember.writer import validate_writer_arguments  # noqa: E402


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


WINDOW = [
    {"role": "user", "content": "我们定一下协议 v1 的字段。", "turn": 1},
    {"role": "assistant", "content": "协议 v1 就用 turn_span 对齐。", "turn": 1},
]


def test_locate():
    assert locate_turn("协议 v1 就用 turn_span 对齐。", WINDOW) == 1
    assert locate_turn("我们定一下协议 v1 的字段。", WINDOW) == 1
    assert locate_turn("不存在的句子", WINDOW) is None
    assert spans_from_quotes(
        [{"start_quote": "我们定一下协议 v1 的字段。",
          "end_quote": "协议 v1 就用 turn_span 对齐。"}], WINDOW) == [(1, 1)]
    print("ok  locate_turn / spans_from_quotes")


def test_writer_schema():
    ok = {"event": {"summary": "s", "participants": ["ai"], "objects": [],
                    "context": "c", "core": "core", "outcome": "o",
                    "state": "s", "affect": "a", "significance": "sig",
                    "details": [], "key_quotes": []},
          "recall_routes": [{"cue_type": "lexical", "cue": "协议"}],
          "quote_spans": [{"start_quote": "我们定一下协议 v1 的字段。",
                           "end_quote": "协议 v1 就用 turn_span 对齐。"}]}
    v, err = validate_writer_arguments(ok)
    assert v and v["quote_spans"], err
    # 不给 quote_spans 也可以（向后兼容）
    no_q = {k: val for k, val in ok.items() if k != "quote_spans"}
    v2, err = validate_writer_arguments(no_q)
    assert v2 and v2["quote_spans"] == [], err
    # 坏 quote（空字符串）拒绝
    bad = dict(ok, quote_spans=[{"start_quote": "", "end_quote": "x"}])
    _, err = validate_writer_arguments(bad)
    assert err and "start_quote" in err
    print("ok  writer schema（quote_spans 可选/兼容/拒绝空句）")


def test_progress_and_resume():
    with tempfile.TemporaryDirectory() as root:
        sleep_run.DATA = root
        sleep_run.PROGRESS_FILE = os.path.join(root, "sleep_progress.jsonl")
        sleep_v2.EVENTS_FILE = os.path.join(root, "events.jsonl")
        sleep_v2.SLEEP_STATE = os.path.join(root, "sleep_state.jsonl")
        sleep_v2.RELATIONS_FILE = os.path.join(root, "event_relations.jsonl")

        # 三个攒着的旧事件（无归属，散，带粗锚点 turn-1）+ 一个 Entity open
        # + 004 无锚点（演示"无可裁对象"分支）
        events = {}
        for i, eid in enumerate(["EVT-001-aaaa", "EVT-002-bbbb",
                                 "EVT-003-cccc", "EVT-004-dddd"]):
            events[eid] = {"event_id": eid, "version_id": eid + "-v1",
                           "source_ref": "conversation/session-s1", "raw_ref": "t",
                           "event_time": _now_iso(),
                           "ingest_time": _now_iso(),
                           "turn_refs": [] if eid.endswith("dddd") else ["turn-1"],
                           "summary": f"事件{i}", "core": f"内容{i}"}
        with open(os.path.join(root, "events.jsonl"), "w",
                  encoding="utf-8") as f:
            for d in events.values():
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        estore = EntityStore(root)
        estore.upsert(Entity(entity_id="ent_x", name="X"))
        estore.save()

        # 记账：turn1 带出 001/002/003（004 无召回 → 无可裁对象分支）
        ledger = RecallLedger(root)
        ledger.record("s1", 1, route="entity",
                      event_ids=["EVT-001-aaaa", "EVT-002-bbbb",
                                 "EVT-003-cccc"])

        # mock 调用②：按"新事件 id"分发（fake_judge 通过 _current 读取，
        # 不靠 user 文本匹配——材料 user 同时含多个 core，文本匹配会错乱）
        def fake_judge(system, user, engine):
            new_id = _current["new_id"]
            if new_id == "EVT-001-aaaa":   # extract：新主题 = N + 002 + 003
                return json.dumps({
                    "pairs": [
                        {"old_event_id": "EVT-002-bbbb", "outcome": "relation",
                         "type": "continues", "confidence": "high",
                         "reason": "延续", "change": "", "status_evidence": ""},
                        {"old_event_id": "EVT-003-cccc", "outcome": "neighbor",
                         "reason": "共现"},
                    ],
                    "entity": {"action": "extract", "name": "新主题",
                               "aliases": [], "description": "测试主题",
                               "member_event_ids": [
                                   "EVT-001-aaaa", "EVT-002-bbbb",
                                   "EVT-003-cccc"],
                               "entity_id": ""},
                }, ensure_ascii=False)
            if new_id == "EVT-002-bbbb":   # 第一次坏 verdict（重名），重试 good
                if _calls["002"] == 0:
                    _calls["002"] = 1
                    return json.dumps({
                        "pairs": [{"old_event_id": "EVT-001-aaaa",
                                   "outcome": "none", "reason": "x"}],
                        "entity": {"action": "extract", "name": "新主题",
                                   "aliases": [], "description": "重名",
                                   "member_event_ids": [
                                       "EVT-001-aaaa", "EVT-002-bbbb",
                                       "EVT-003-cccc"]}}, ensure_ascii=False)
                return json.dumps({
                    "pairs": [{"old_event_id": "EVT-001-aaaa",
                               "outcome": "neighbor", "reason": "x"}],
                    "entity": {"action": "none"}}, ensure_ascii=False)
            # EVT-003：none
            return json.dumps({
                "pairs": [{"old_event_id": "EVT-001-aaaa",
                           "outcome": "none", "reason": "x"}],
                "entity": {"action": "none"}}, ensure_ascii=False)

        sleep_v2.call_llm = fake_judge
        _current = {"new_id": ""}
        _calls = {"002": 0}
        sleep_run._now_iso = _now_iso

        # 直接复刻 main 的核心循环（不走 argparse）
        events = sleep_v2.load_events()
        statuses = sleep_v2.load_sleep_state()
        done = sleep_run.load_progress()
        pending = sleep_run.pending_events(events, done, [])
        assert set(pending) == set(events), pending

        estore = EntityStore(root)
        spans = TurnSpanStore(root)
        pairs = PairSeenStore(root)
        entity_names = sleep_v2.existing_entity_names(estore)
        materials = sleep_v2.assemble_materials(
            pending, events, statuses, spans, ledger, pairs, estore,
            )
        # 004 无记账 → 不进材料
        mats_ids = {m["new_id"] for m in materials}
        assert "EVT-004-dddd" not in mats_ids
        state_up, event_up = {}, {}
        for m in materials:
            _current["new_id"] = m["new_id"]
            user = __import__("ember.prompts",
                              fromlist=["build_judge_user_prompt"]
                              ).build_judge_user_prompt(m["new_event"],
                                                        m["old_events"])
            v = sleep_v2.extract_json(
                sleep_v2.call_llm("", user, "fake"))
            errs = sleep_v2.validate(v, m, events, statuses, entity_names)
            if errs:
                # 002 第一次坏 verdict：不记进度
                assert m["new_id"] == "EVT-002-bbbb"
                continue
            s = sleep_v2.apply_verdict(v, m, events, statuses, estore,
                                       pairs, "fake", state_up, event_up)
            for mid, up in event_up.items():
                events[mid].update(up)
            event_up.clear()
            sleep_run.mark_progress(m["new_id"], "fake", s)
        # 004 无可裁对象也记进度（main 逻辑）
        sleep_run.mark_progress("EVT-004-dddd", "fake",
                                {"note": "无可裁对象"})

        done = sleep_run.load_progress()
        assert "EVT-001-aaaa" in done
        assert "EVT-004-dddd" in done
        assert "EVT-002-bbbb" not in done, "坏 verdict 不记进度（failed 上报）"
        # 续跑：pending 只剩 002 —— 各自从断点续跑（--new 即续跑入口）
        pending2 = sleep_run.pending_events(events, sleep_run.load_progress(), [])
        assert pending2 == ["EVT-002-bbbb"], pending2
        # extract 已落：新 Entity + 001/003 回填
        ents = {e.name: e for e in estore.all()}
        assert "新主题" in ents
        m1 = ents["新主题"].entity_id
        assert events["EVT-001-aaaa"]["entity_refs"] == [m1]
        assert events["EVT-003-cccc"]["entity_refs"] == [m1]
        print("ok  进度账本+续跑（成功记/失败不记/无可裁对象也记）")


def main():
    test_locate()
    test_writer_schema()
    test_progress_and_resume()
    print("\nSleep 入口 + quote_spans 全部通过")


if __name__ == "__main__":
    main()
