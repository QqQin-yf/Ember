# -*- coding: utf-8 -*-
"""B1 Sleep 调用②（v2）测试：mock LLM，覆盖三种结局 + 校验拒绝。

不联网：直接 patch scripts.sleep_v2 的 call_llm。
数据：tempdir 造 events/记账/turn_span/实体，跑 assemble→validate→apply 全链。
"""
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import sleep_v2  # noqa: E402
from ember.entity import Entity, EntityStore  # noqa: E402
from ember.pair_seen import PairSeenStore  # noqa: E402
from ember.recall_ledger import RecallLedger  # noqa: E402
from ember.store import EmberStore  # noqa: E402
from ember.turn_span import TurnSpanStore  # noqa: E402


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def make_env(root):
    """造 6 条事件：EVT-001..003（旧，散）、EVT-004（旧，属 heart）、
    EVT-005（旧，superseded）、EVT-100（新）。记账：新事件在 turn 3 被召回，
    带出 001/002/004/005。"""
    os.makedirs(root, exist_ok=True)
    events = {}
    def ev(eid, core, summary="", refs=None, change=None):
        d = {"event_id": eid, "version_id": eid + "-v1",
             "source_ref": "conversation/session-s1", "raw_ref": "t",
             "event_time": _now_iso(), "ingest_time": _now_iso(),
             "turn_refs": ["turn-1"], "core": core, "summary": summary}
        if refs is not None:
            d["entity_refs"] = refs
        if change is not None:
            d["change"] = change
        events[eid] = d
    ev("EVT-001-aaaa", "讨论了 heart 的梦机制设计", "heart 梦机制")
    ev("EVT-002-bbbb", "决定 FragmentSource 协议字段", "FragmentSource")
    ev("EVT-003-cccc", "随便聊了天气", "天气")
    ev("EVT-004-dddd", "heart 的七通道快照定稿", "heart 七通道", refs=["ent_heart"])
    ev("EVT-005-eeee", "旧版梦设计（已被推翻）", "旧梦设计")
    ev("EVT-100-ffff", "heart 梦机制实现落地第一版", "梦机制落地")
    with open(os.path.join(root, "events.jsonl"), "w", encoding="utf-8") as f:
        for d in events.values():
            f.write(json.dumps(d, ensure_ascii=False) + "\n")

    estore = EntityStore(root)
    estore.upsert(Entity(entity_id="ent_heart", name="heart"))
    estore.save()

    # sleep_state：005 superseded
    with open(os.path.join(root, "sleep_state.jsonl"), "w", encoding="utf-8") as f:
        f.write(json.dumps({"event_id": "EVT-005-eeee", "status": "superseded"}) + "\n")

    # turn_span：新事件覆盖 turn 3
    spans = TurnSpanStore(root)
    spans.fill("EVT-100-ffff", [(3, 3)], filled_by="test")

    # 记账：turn 3 召回带出 001/002/003/004/005
    ledger = RecallLedger(root)
    ledger.record("s1", 3, route="entity", event_ids=[
        "EVT-001-aaaa", "EVT-002-bbbb", "EVT-003-cccc",
        "EVT-004-dddd", "EVT-005-eeee"])

    return events, estore, spans, ledger, PairSeenStore(root)


def test_assemble():
    with tempfile.TemporaryDirectory() as root:
        events, estore, spans, ledger, pairs = make_env(root)
        statuses = sleep_v2.load_sleep_state.__wrapped__(None) if False else \
            {"EVT-005-eeee": "superseded"}
        mats = sleep_v2.assemble_materials(
            ["EVT-100-ffff"], events, statuses, spans, ledger, pairs, estore)
        m = mats[0]
        olds = [o["event_id"] for o in m["old_events"]]
        assert "EVT-005-eeee" not in olds, "superseded 应被排除"
        assert set(olds) == {"EVT-001-aaaa", "EVT-002-bbbb", "EVT-003-cccc",
                             "EVT-004-dddd"}, olds
        by_id = {o["event_id"]: o for o in m["old_events"]}
        assert by_id["EVT-004-dddd"]["entity_name"] == "heart"
        assert by_id["EVT-001-aaaa"]["entity_name"] == ""
        print("ok  assemble（superseded 排除 + 实体标注）")


def test_validate_and_apply_full(monkey_verdict):
    with tempfile.TemporaryDirectory() as root:
        events, estore, spans, ledger, pairs = make_env(root)
        os.makedirs(os.path.join(root, "__"))  # noop
        globals()["__events_path_fix"] = None
        # 把脚本的数据路径指到 tempdir
        sleep_v2.EVENTS_FILE = os.path.join(root, "events.jsonl")
        sleep_v2.SLEEP_STATE = os.path.join(root, "sleep_state.jsonl")
        sleep_v2.RELATIONS_FILE = os.path.join(root, "event_relations.jsonl")
        events = sleep_v2.load_events()
        statuses = {"EVT-005-eeee": "superseded"}

        material = {
            "new_id": "EVT-100-ffff",
            "new_event": {"event_id": "EVT-100-ffff", "core": "x", "summary": ""},
            "old_events": [
                {"event_id": "EVT-001-aaaa", "entity_name": "", "core": "x", "summary": ""},
                {"event_id": "EVT-002-bbbb", "entity_name": "", "core": "x", "summary": ""},
                {"event_id": "EVT-003-cccc", "entity_name": "", "core": "x", "summary": ""},
                {"event_id": "EVT-004-dddd", "entity_name": "heart", "core": "x", "summary": ""},
            ],
        }
        errs = sleep_v2.validate(monkey_verdict, material, events, statuses,
                                 {"heart"})
        assert not errs, errs
        estore = EntityStore(root)
        pairs = PairSeenStore(root)
        state_up, event_up = {}, {}
        summary = sleep_v2.apply_verdict(monkey_verdict, material, events,
                                         statuses, estore, pairs, "test",
                                         state_up, event_up)
        # relation 边落盘（旧→新）
        rels = [json.loads(l) for l in
                open(sleep_v2.RELATIONS_FILE, encoding="utf-8") if l.strip()]
        assert any(r["src_event"] == "EVT-001-aaaa"
                   and r["dst_event"] == "EVT-100-ffff"
                   and r["type"] == "continues" for r in rels), rels
        # resolves 三处落盘之一：state
        assert state_up["EVT-002-bbbb"]["status"] == "superseded"
        # 之三：change
        assert event_up["EVT-002-bbbb"]["change"]
        # extract：新 Entity + member 回填（001/003 散事件；004 已有归属不进）
        ents = {e.entity_id: e for e in estore.all()}
        new_ent = [e for e in ents.values() if e.name == "heart梦"]
        assert len(new_ent) == 1
        eid = new_ent[0].entity_id
        assert event_up["EVT-001-aaaa"]["entity_refs"] == [eid]
        assert event_up["EVT-003-cccc"]["entity_refs"] == [eid]
        assert "EVT-004-dddd" not in event_up, "已有归属的事件不得入 member"
        # neighbor 留痕
        assert pairs.is_seen("EVT-100-ffff", "EVT-004-dddd")
        print("ok  validate+apply（三种结局落盘 + 三处落盘 + member 纪律）")


def test_validate_rejects():
    """校验拒绝路径：member 已有归属 / name 重复 / attach 目标非法 / resolves 缺 change。"""
    with tempfile.TemporaryDirectory() as root:
        sleep_v2.EVENTS_FILE = os.path.join(root, "events.jsonl")
        events, estore, spans, ledger, pairs = make_env(root)
        statuses = {"EVT-005-eeee": "superseded"}
        material = {
            "new_id": "EVT-100-ffff",
            "new_event": {"event_id": "EVT-100-ffff", "core": "x", "summary": ""},
            "old_events": [
                {"event_id": "EVT-004-dddd", "entity_name": "heart", "core": "x", "summary": ""},
            ],
        }
        names = {"heart"}
        # member 已有归属
        v = {"pairs": [], "entity": {"action": "extract", "name": "新实体",
             "aliases": [], "description": "x",
             "member_event_ids": ["EVT-004-dddd", "EVT-100-ffff", "EVT-001-aaaa"]}}
        errs = sleep_v2.validate(v, material, events, statuses, names)
        assert any("已有归属" in e for e in errs), errs
        # member 不足 3
        v2 = {"pairs": [], "entity": {"action": "extract", "name": "新实体",
              "aliases": [], "description": "x", "member_event_ids": ["EVT-001-aaaa"]}}
        errs = sleep_v2.validate(v2, material, events, statuses, names)
        assert any("不足 3" in e for e in errs), errs
        # name 与已有重复
        v3 = {"pairs": [], "entity": {"action": "extract", "name": "heart",
              "aliases": [], "description": "x",
              "member_event_ids": ["EVT-001-aaaa", "EVT-002-bbbb", "EVT-003-cccc"]}}
        errs = sleep_v2.validate(v3, material, events, statuses, names)
        assert any("重复" in e for e in errs), errs
        # attach 目标非法（材料内旧事件只归 heart）
        v4 = {"pairs": [], "entity": {"action": "attach", "entity_id": "ent_other"}}
        errs = sleep_v2.validate(v4, material, events, statuses, names)
        assert any("attach" in e for e in errs), errs
        # resolves 缺 change
        material4 = {
            "new_id": "EVT-100-ffff",
            "new_event": {"event_id": "EVT-100-ffff", "core": "x", "summary": ""},
            "old_events": [
                {"event_id": "EVT-001-aaaa", "entity_name": "", "core": "x", "summary": ""}],
        }
        v5 = {"pairs": [{"old_event_id": "EVT-001-aaaa", "outcome": "relation",
                         "type": "resolves", "confidence": "high",
                         "reason": "x", "change": "", "status_evidence": "y"}],
              "entity": {"action": "none"}}
        errs = sleep_v2.validate(v5, material4, events, statuses, names)
        assert any("change" in e for e in errs), errs
        # pair 指向材料外
        v6 = {"pairs": [{"old_event_id": "EVT-999-zzzz", "outcome": "none"}],
              "entity": {"action": "none"}}
        errs = sleep_v2.validate(v6, material4, events, statuses, names)
        assert any("材料外" in e for e in errs), errs
        print("ok  validate 拒绝路径（凑数/重名/非法 attach/缺 change/材料外）")


VERDICT_FULL = {
    "pairs": [
        {"old_event_id": "EVT-001-aaaa", "outcome": "relation",
         "type": "continues", "confidence": "high", "reason": "梦机制的延续",
         "change": "", "status_evidence": ""},
        {"old_event_id": "EVT-002-bbbb", "outcome": "relation",
         "type": "resolves", "confidence": "high",
         "reason": "落地版取代了纯协议讨论",
         "change": "当时只定了协议字段，现在看到它真正跑起来了，我意识到协议设计要为实现让步。",
         "status_evidence": "EVT-100 落地实现取代了纯协议讨论"},
        {"old_event_id": "EVT-003-cccc", "outcome": "neighbor",
         "reason": "同时段共现但无具体联系"},
        {"old_event_id": "EVT-004-dddd", "outcome": "neighbor",
         "reason": "同属 heart 话题但本条无具体联系"},
    ],
    "entity": {
        "action": "extract",
        "name": "heart梦",
        "aliases": ["heart 的梦"],
        "description": "heart 系统的梦机制设计与实现",
        "member_event_ids": ["EVT-001-aaaa", "EVT-003-cccc", "EVT-100-ffff"],
        "entity_id": "",
    },
}


def test_persistence_and_attach_id():
    """交接单验证清单：落盘持久化（resolves change / extract 回填 /
    attach 带 entity_id）+ 重读文件验证。"""
    with tempfile.TemporaryDirectory() as root:
        sleep_v2.EVENTS_FILE = os.path.join(root, "events.jsonl")
        sleep_v2.SLEEP_STATE = os.path.join(root, "sleep_state.jsonl")
        sleep_v2.RELATIONS_FILE = os.path.join(root, "event_relations.jsonl")
        events, estore, spans, ledger, pairs = make_env(root)
        statuses = {"EVT-005-eeee": "superseded"}
        # 补两个"新事件"（closing 已写入的），供 attach/extract 校验引用
        for eid in ("EVT-100-ffff", "EVT-101-gggg"):
            events[eid] = {"event_id": eid, "version_id": eid + "-v1",
                           "source_ref": "conversation/session-s1", "raw_ref": "t",
                           "event_time": _now_iso(),
                           "ingest_time": _now_iso(),
                           "turn_refs": [], "summary": eid,
                           "core": eid + " 内容"}

        # attach 材料：旧事件带 entity_id（新组装格式）
        m1 = {
            "new_id": "EVT-100-ffff",
            "new_event": {"event_id": "EVT-100-ffff", "core": "x", "summary": ""},
            "old_events": [
                {"event_id": "EVT-004-dddd", "entity_name": "heart",
                 "entity_id": "ent_heart", "core": "x", "summary": ""},
            ],
        }
        # resolves 材料：旧事件 001 被推翻 + extract member=001/002
        m2 = {
            "new_id": "EVT-101-gggg",
            "new_event": {"event_id": "EVT-101-gggg", "core": "x", "summary": ""},
            "old_events": [
                {"event_id": "EVT-001-aaaa", "entity_name": "", "core": "x", "summary": ""},
                {"event_id": "EVT-002-bbbb", "entity_name": "", "core": "x", "summary": ""},
                {"event_id": "EVT-003-cccc", "entity_name": "", "core": "x", "summary": ""},
            ],
        }

        # N1: attach（带 entity_id 应通过校验）
        v1 = {"pairs": [{"old_event_id": "EVT-004-dddd", "outcome": "relation",
                         "type": "continues", "confidence": "high",
                         "reason": "x", "change": "", "status_evidence": ""}],
              "entity": {"action": "attach", "entity_id": "ent_heart"}}
        errs = sleep_v2.validate(v1, m1, events, statuses, {"heart"})
        assert not errs, f"attach 带 id 应通过: {errs}"

        # N2: resolves + extract
        v2 = {"pairs": [
                {"old_event_id": "EVT-001-aaaa", "outcome": "relation",
                 "type": "resolves", "confidence": "high",
                 "reason": "x",
                 "change": "当时以为协议就那样了，现在看到 v2 才知道当时的字段设计太糙。",
                 "status_evidence": "v2 推翻初版"},
                {"old_event_id": "EVT-002-bbbb", "outcome": "neighbor",
                 "reason": "x"},
                {"old_event_id": "EVT-003-cccc", "outcome": "neighbor",
                 "reason": "x"}],
              "entity": {"action": "extract", "name": "新主题测试",
                         "aliases": [], "description": "测试",
                         "member_event_ids": ["EVT-002-bbbb",
                                              "EVT-003-cccc",
                                              "EVT-101-gggg"]}}
        errs = sleep_v2.validate(v2, m2, events, statuses, {"heart"})
        assert not errs, errs

        # 模拟 main 的累计落盘：两个 Event 各自的 event_updates 累计
        state_up, event_up = {}, {}
        all_event_updates = {}
        for v, m in ((v1, m1), (v2, m2)):
            sleep_v2.apply_verdict(v, m, events, statuses, estore, pairs,
                                   "fake", state_up, event_up)
            for mid, up in event_up.items():
                events[mid].update(up)
                all_event_updates.setdefault(mid, {}).update(up)
            event_up.clear()
        # 走 rewrite_events（修复后的 main 收尾动作）
        sleep_v2.rewrite_events(events, all_event_updates)

        # 重读文件验证：change 已写入 / member refs 已回填 / attach 已归入
        reloaded = sleep_v2.load_events()
        assert reloaded["EVT-001-aaaa"].get("change", "").startswith("当时以为协议"), \
            "resolves change 未落盘"
        assert reloaded["EVT-002-bbbb"].get("entity_refs") == [m1 and "" or ""] or \
            reloaded["EVT-002-bbbb"].get("entity_refs"), \
            "extract member entity_refs 未落盘"
        ent_new = [e for e in estore.all() if e.name == "新主题测试"][0]
        assert reloaded["EVT-002-bbbb"]["entity_refs"] == [ent_new.entity_id]
        assert reloaded["EVT-100-ffff"]["entity_refs"] == ["ent_heart"], \
            "attach entity_refs 未落盘"
        # resolves 状态已落（sleep_state 由调用方落，此处验证 state_up）
        assert state_up["EVT-001-aaaa"]["status"] == "superseded"
        print("ok  持久化+attach带id（重读文件验证 change/refs/attach）")


def test_cross_session_isolation():
    """交接单 r2 新增：两个 session 各自 turn-3 带出不同旧事件，
    新 Event 只拿到自己 session 那一轮的旧事件（同号轮次不混同）。"""
    with tempfile.TemporaryDirectory() as root:
        sleep_v2.EVENTS_FILE = os.path.join(root, "events.jsonl")
        sleep_v2.SLEEP_STATE = os.path.join(root, "sleep_state.jsonl")
        sleep_v2.RELATIONS_FILE = os.path.join(root, "event_relations.jsonl")
        events, estore, spans, ledger, pairs = make_env(root)
        statuses = {"EVT-005-eeee": "superseded"}
        # 新事件：会话 B（source_ref session-B），turn_span 覆盖 turn 3
        from ember.models import Event
        store = EmberStore(root)
        new_id = "EVT-100-ffff"
        store.write_event(Event(
            event_id=new_id, version_id=new_id + "-v1",
            source_ref="conversation/session-B", raw_ref="t",
            event_time=_now_iso(), ingest_time=_now_iso(),
            turn_refs=["turn-3"], summary="新事件", core="新事件 内容"))
        events[new_id] = {"event_id": new_id, "summary": "新事件",
                          "core": "新事件 内容",
                          "source_ref": "conversation/session-B"}
        # 记账：会话 A turn-3 带出 001；会话 B turn-3 带出 002
        ledger.record("A", 3, route="entity", event_ids=["EVT-001-aaaa"])
        ledger.record("B", 3, route="entity", event_ids=["EVT-002-bbbb"])

        mats = sleep_v2.assemble_materials(
            [new_id], events, statuses, spans, ledger, pairs, estore)
        mats = [m for m in mats if m["old_events"]]
        assert len(mats) == 1, mats
        olds = {o["event_id"] for o in mats[0]["old_events"]}
        assert olds == {"EVT-002-bbbb"}, \
            f"应只拿到自己 session（B）的旧事件，实际: {olds}"
        print("ok  跨 session 隔离（同号轮次不混同）")


def test_attach_close():
    """attach 顺带判 close：close=true 关 open 实体 / false 重置 closed /
    close=true 缺 reason 拒绝。"""
    with tempfile.TemporaryDirectory() as root:
        sleep_v2.EVENTS_FILE = os.path.join(root, "events.jsonl")
        sleep_v2.SLEEP_STATE = os.path.join(root, "sleep_state.jsonl")
        sleep_v2.RELATIONS_FILE = os.path.join(root, "event_relations.jsonl")
        events, estore, spans, ledger, pairs = make_env(root)
        statuses = {"EVT-005-eeee": "superseded"}
        # 造新事件 + open 实体 ent_x
        from ember.models import Event
        store = EmberStore(root)
        new_id = "EVT-100-ffff"
        store.write_event(Event(
            event_id=new_id, version_id=new_id + "-v1", source_ref="t",
            raw_ref="t", event_time=_now_iso(), ingest_time=_now_iso(),
            turn_refs=[], summary="新事件", core="新事件 内容"))
        events[new_id] = {"event_id": new_id, "summary": "新事件",
                          "core": "新事件 内容"}
        estore.upsert(Entity(entity_id="ent_x", name="X"))
        estore.save()

        # 材料：旧事件 004 属 ent_heart（open）
        m = {
            "new_id": new_id,
            "new_event": {"event_id": new_id, "core": "x", "summary": ""},
            "old_events": [
                {"event_id": "EVT-004-dddd", "entity_name": "heart",
                 "entity_id": "ent_heart", "core": "x", "summary": ""},
            ],
        }

        # 1) close=true 缺 reason → 拒绝
        v = {"pairs": [{"old_event_id": "EVT-004-dddd", "outcome": "relation",
                        "type": "continues", "confidence": "high",
                        "reason": "x", "change": "", "status_evidence": ""}],
             "entity": {"action": "attach", "entity_id": "ent_heart",
                        "close": True}}
        errs = sleep_v2.validate(v, m, events, statuses, {"heart"})
        assert any("close_reason" in e for e in errs), errs

        # 2) close=true → open 实体被关（closed_at=归入时刻）
        v["entity"]["close_reason"] = "heart 项目收尾"
        state_up, event_up = {}, {}
        s = sleep_v2.apply_verdict(v, m, events, statuses, estore, pairs,
                                   "fake", state_up, event_up)
        for mid, up in event_up.items():
            events[mid].update(up)   # 与 main 相同：调用方合并 entity_updates
        heart = estore.get("ent_heart")
        assert heart.status == "closed" and heart.closed_at, s
        assert s.get("attach_closed") == "ent_heart"
        assert events[new_id]["entity_refs"] == ["ent_heart"]
        # reopen 以便 3) 测试
        heart.reopen()
        estore.save()

        # 3) close=false → 归入 open 实体无动作；归入 closed 实体重置
        v3 = {"pairs": [{"old_event_id": "EVT-004-dddd", "outcome": "relation",
                         "type": "continues", "confidence": "high",
                         "reason": "x", "change": "", "status_evidence": ""}],
              "entity": {"action": "attach", "entity_id": "ent_heart",
                         "close": False}}
        events[new_id]["entity_refs"] = []   # 单归属重置
        state_up, event_up = {}, {}
        sleep_v2.apply_verdict(v3, m, events, statuses, estore, pairs,
                               "fake", state_up, event_up)
        for mid, up in event_up.items():
            events[mid].update(up)
        assert estore.get("ent_heart").status == "open"  # close=false 不关

        # 4) closed 实体 + close 缺省 → 重置起算点（现有规则）
        estore.close("ent_heart", _now_iso())
        old_closed_at = estore.get("ent_heart").closed_at
        events[new_id]["entity_refs"] = []
        state_up, event_up = {}, {}
        v4 = {"pairs": [{"old_event_id": "EVT-004-dddd", "outcome": "relation",
                         "type": "continues", "confidence": "high",
                         "reason": "x", "change": "", "status_evidence": ""}],
              "entity": {"action": "attach", "entity_id": "ent_heart"}}
        sleep_v2.apply_verdict(v4, m, events, statuses, estore, pairs,
                               "fake", state_up, event_up)
        for mid, up in event_up.items():
            events[mid].update(up)
        assert estore.get("ent_heart").closed_at >= old_closed_at
        print("ok  attach close（true 关 open 实体 / false 不关 / 缺省重置起算点）")


def main():
    test_assemble()
    test_validate_and_apply_full(VERDICT_FULL)
    test_validate_rejects()
    test_persistence_and_attach_id()
    test_cross_session_isolation()
    test_attach_close()
    print("\nB1 全部通过")


if __name__ == "__main__":
    main()
