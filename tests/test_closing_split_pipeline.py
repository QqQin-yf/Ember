# -*- coding: utf-8 -*-
"""三份新脚本测试：closing_write / split_directions / pipeline。

不联网：LLM 结果以注入 verdict 的方式直测核心函数；
pipeline 串联用 monkeypatch 掉 call_llm。
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import closing_write as cw  # noqa: E402
import split_directions as sd  # noqa: E402
import pipeline  # noqa: E402
from ember.entity import Entity, EntityStore  # noqa: E402
from ember.store import EmberStore  # noqa: E402
from ember.turn_span import TurnSpanStore  # noqa: E402
from ember.recall_ledger import RecallLedger  # noqa: E402


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


WINDOW = [
    {"role": "user", "content": "我们聊聊 heart 的梦机制吧，设计稿我看过了", "turn": 1},
    {"role": "assistant", "content": "好，梦机制的入口我建议从 FragmentSource 协议开始。", "turn": 1},
    {"role": "user", "content": "今天定一下：FragmentSource 协议 v1 就用 turn_span 对齐。", "turn": 2},
    {"role": "assistant", "content": "同意，FragmentSource 协议 v1 就用 turn_span 对齐。", "turn": 2},
    {"role": "user", "content": "顺便问下明天天气", "turn": 3},
    {"role": "assistant", "content": "明天晴。", "turn": 3},
]

VERDICT = {
    "events": [
        {"summary": "FragmentSource 协议 v1 定稿",
         "participants": ["ai", "用户"],
         "objects": [], "context": "heart 项目讨论",
         "core": "我和用户讨论后决定 FragmentSource 协议 v1 用 turn_span 对齐。",
         "outcome": "协议 v1 定稿", "state": "", "affect": "",
         "significance": "heart 梦机制的接口基础",
         "details": [],
         "cues": [
             {"cue_type": "lexical", "cue": "FragmentSource"},
             {"cue_type": "lexical", "cue": "turn_span"},
             {"cue_type": "conceptual", "cue": "协议定稿"},
         ],
         "quote_spans": [
             {"start_quote": "今天定一下：FragmentSource 协议 v1 就用 turn_span 对齐。",
              "end_quote": "同意，FragmentSource 协议 v1 就用 turn_span 对齐。"},
         ]},
    ]
}


def test_locate_and_write():
    with tempfile.TemporaryDirectory() as root:
        cw.DATA = root
        cw.EVENTS_FILE = os.path.join(root, "events.jsonl")
        sd.DATA = root
        sd.EVENTS_FILE = os.path.join(root, "events.jsonl")
        pipeline.DATA = root

        # quote 定位
        t = cw.locate_turn("今天定一下：FragmentSource 协议 v1 就用 turn_span 对齐。",
                           WINDOW)
        assert t == 2, t
        # 空白归一容错
        t2 = cw.locate_turn("今天定一下： FragmentSource 协议 v1 就用 turn_span 对齐。 ",
                            WINDOW)
        assert t2 == 2
        assert cw.locate_turn("不存在的句子", WINDOW) is None

        # 落盘全链
        store = EmberStore(root)
        spans = TurnSpanStore(root)
        r = cw.write_closing(VERDICT, "sess1", WINDOW, store, spans)
        w = r["written"][0]
        assert w["cues"] == 3
        assert w["turn_spans"] == [[2, 2]], w
        assert not w["quote_failed"]
        ev = store.get_event(w["event_id"])
        assert ev.summary == "FragmentSource 协议 v1 定稿"
        assert ev.core.startswith("我和用户讨论")
        routes = store.routes_for_event(w["event_id"])
        assert len(routes) == 3
        assert {rt.cue for rt in routes} == {"FragmentSource", "turn_span", "协议定稿"}
        assert store.index_snapshot().get("fragmentsource")
        print("ok  closing 定位+落盘（Event/Cue/turn_span/索引）")


def test_validate_rejects():
    bad = {"summary": "", "core": "", "cues": [], "quote_spans": []}
    errs = cw.validate_event(bad)
    assert any("缺字段" in e for e in errs)
    assert any("summary 非空" in e for e in errs)
    bad2 = dict(VERDICT["events"][0])
    bad2["cues"] = [{"cue_type": "weird", "cue": "x"}]
    errs = cw.validate_event(bad2)
    assert any("cue_type" in e for e in errs)
    bad3 = dict(VERDICT["events"][0])
    bad3["event_id"] = "EVT-999"   # LLM 偷带系统字段
    errs = cw.validate_event(bad3)
    assert any("未声明字段" in e for e in errs)
    print("ok  closing 校验拒绝（缺字段/非法 cue_type/系统字段）")


def test_split():
    with tempfile.TemporaryDirectory() as root:
        cw.DATA = root
        cw.EVENTS_FILE = os.path.join(root, "events.jsonl")
        sd.DATA = root
        sd.EVENTS_FILE = os.path.join(root, "events.jsonl")
        # 8 条事件属 ent_x
        events = {}
        for i in range(1, 9):
            eid = f"EVT-{i:03d}-aaaa"
            events[eid] = {"event_id": eid, "summary": f"架构讨论{i}",
                           "entity_refs": ["ent_x"]}
        with open(os.path.join(root, "events.jsonl"), "w", encoding="utf-8") as f:
            for d in events.values():
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        estore = EntityStore(root)
        estore.upsert(Entity(entity_id="ent_x", name="X记忆系统"))
        estore.save()

        verdict = {"directions": [
            {"description": "它的架构与机制如何设计、为什么这样设计"},
            {"description": "它如何认识自己"},
        ], "reason": "两条主线"}
        assert not sd.validate(verdict)
        assert sd.validate({"directions": [], "reason": "x"})
        assert sd.validate({"directions": [{"description": ""}], "reason": "x"})

        # 模拟 main 的落盘段
        entity = estore.get("ent_x")
        for i, d in enumerate(verdict["directions"]):
            entity.directions.append(
                sd.Direction(direction_id=f"dir_x_{i+1}",
                             description=d["description"]))
        estore.save()
        estore2 = EntityStore(root)
        assert len(estore2.get("ent_x").directions) == 2
        assert estore2.get("ent_x").directions[0].direction_id == "dir_x_1"
        print("ok  split 校验+落盘（dir id 生成/持久化）")


def test_pipeline(monkeypatch):
    with tempfile.TemporaryDirectory() as root:
        cw.DATA = root
        cw.EVENTS_FILE = os.path.join(root, "events.jsonl")
        cw.SESSIONS_FILE = os.path.join(root, "sessions.jsonl")
        sd.DATA = root
        sd.EVENTS_FILE = os.path.join(root, "events.jsonl")
        pipeline.DATA = root
        # 会话窗口
        with open(os.path.join(root, "sessions.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"session_id": "sess1", "messages": WINDOW}) + "\n")
        # 旧事件 + 记账：新事件召回带出旧事件（供第二步有料）
        events = {"EVT-001-aaaa": {
            "event_id": "EVT-001-aaaa", "version_id": "EVT-001-aaaa-v1",
            "source_ref": "t", "raw_ref": "t",
            "event_time": _now_iso(), "ingest_time": _now_iso(),
            "summary": "旧协议讨论", "core": "旧协议讨论"}}
        with open(os.path.join(root, "events.jsonl"), "w", encoding="utf-8") as f:
            for d in events.values():
                f.write(json.dumps(d, ensure_ascii=False) + "\n")
        ledger = RecallLedger(root)
        ledger.record("sess1", 2, route="entity", event_ids=["EVT-001-aaaa"])

        # monkeypatch 两个 LLM 调用
        cw.call_llm = lambda system, user, engine: json.dumps(VERDICT)
        import sleep_v2
        sleep_v2.EVENTS_FILE = os.path.join(root, "events.jsonl")
        sleep_v2.SLEEP_STATE = os.path.join(root, "sleep_state.jsonl")
        sleep_v2.RELATIONS_FILE = os.path.join(root, "event_relations.jsonl")
        calls = {"judge": 0}
        from ember.entity import EntityStore as _ES
        from ember.turn_span import TurnSpanStore as _TSS
        from ember.store import EmberStore as _EmberStore
        orig = sleep_v2.call_llm
        sleep_v2.call_llm = lambda system, user, engine: calls.__setitem__(
            "judge", calls["judge"] + 1) or json.dumps({
                "pairs": [{"old_event_id": "EVT-001-aaaa", "outcome": "relation",
                           "type": "continues", "confidence": "high",
                           "reason": "延续", "change": "", "status_evidence": ""}],
                "entity": {"action": "none"}})
        try:
            # 手动复刻 main 的核心三步（不走 argparse）
            report = {"steps": {}}
            # 1) closing
            r = cw.write_closing(VERDICT, "sess1", WINDOW,
                                 _EmberStore(root), _TSS(root))
            new_ids = [w["event_id"] for w in r["written"]]
            report["steps"]["closing"] = new_ids
            # 2) judge（同 main.do_judge 的核心路径）
            from ember.pair_seen import PairSeenStore
            from ember.prompts import build_judge_user_prompt
            estore = _ES(root)
            spans = _TSS(root)
            pairs = PairSeenStore(root)
            events = sleep_v2.load_events()
            statuses = sleep_v2.load_sleep_state()
            mats = sleep_v2.assemble_materials(
                new_ids, events, statuses, spans, ledger, pairs, estore,
                session_id="sess1")
            mats = [m for m in mats if m["old_events"]]
            assert mats, "调用①新事件应当能组装出材料"
            s = None
            for m in mats:
                user = build_judge_user_prompt(m["new_event"], m["old_events"])
                v = sleep_v2.extract_json(
                    sleep_v2.call_llm(sleep_v2.JUDGE_SYSTEM_PROMPT, user, "test"))
                state_up, event_up = {}, {}
                s = sleep_v2.apply_verdict(v, m, events, statuses, estore,
                                           pairs, "test", state_up, event_up)
                for mid, up in event_up.items():
                    events[mid].update(up)
                if state_up:
                    sleep_v2.SLEEP_STATE = os.path.join(root, "sleep_state.jsonl")
                    for eid, up in state_up.items():
                        with open(sleep_v2.SLEEP_STATE, "a", encoding="utf-8") as f:
                            f.write(json.dumps({"event_id": eid, **up}) + "\n")
            assert s["relations"], s
            # 3) tighten
            from ember.cue_tighten import CueTightener
            t = CueTightener(EmberStore(root), ledger,
                             os.path.join(root, "events.jsonl"),
                             entity_store=estore)
            r3 = t.tighten()
            assert "checked_entities" in r3
        finally:
            sleep_v2.call_llm = orig
        print("ok  pipeline 三步串联（closing→judge→tighten，全 mock）")


def main():
    test_locate_and_write()
    test_validate_rejects()
    test_split()
    test_pipeline(None)
    print("\n三份新脚本全部通过")


if __name__ == "__main__":
    main()
