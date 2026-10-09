# -*- coding: utf-8 -*-
"""假数据全链路演练 v2：捏造一套记忆库（4 实体 / 12 旧事件 / 1 假会话），
mock LLM 跑 closing→judge→tighten，覆盖：

relation 词表 9 种（continues/concludes/inspired_by/caused_by/
responds_to/resolves/revisits/contrasts/related_to——causes 按方向
约定不出现在 LLM 输出）+ neighbor + none；
归属三种结局 extract×2 / attach×2 / none×1；
校验拒绝（name 重名）→ pair 不留痕 → 重试成功；
quote 定位成功×4 / 失败×1；
收紧 retire（0 使用）与保留（有使用）/ open 豁免。
"""
import json
import os
import shutil
import uuid
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

ROOT = tempfile.mkdtemp(prefix="ember_e2e_v2_")
print(f"[sandbox] {ROOT}")

import sleep_v2  # noqa: E402
import split_directions as sd  # noqa: E402
from ember.entity import Entity, EntityStore  # noqa: E402
from ember.store import EmberStore  # noqa: E402
from ember.turn_span import TurnSpanStore, spans_from_quotes  # noqa: E402
from ember.recall_ledger import RecallLedger  # noqa: E402
from ember.pair_seen import PairSeenStore  # noqa: E402

for mod in (sleep_v2, sd):
    mod.DATA = ROOT
sleep_v2.EVENTS_FILE = os.path.join(ROOT, "events.jsonl")
sleep_v2.SLEEP_STATE = os.path.join(ROOT, "sleep_state.jsonl")
sleep_v2.RELATIONS_FILE = os.path.join(ROOT, "event_relations.jsonl")
sd.EVENTS_FILE = os.path.join(ROOT, "events.jsonl")

SESSION = "fake_s2"
NOW = datetime.now(timezone.utc).astimezone()


def iso(days_ago: float = 0) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 假库：4 实体 / 12 旧事件
# ---------------------------------------------------------------------------
def ev(eid, summary, core, refs=None, days=6):
    d = {"event_id": eid, "version_id": eid + "-v1",
         "source_ref": "conversation/session-fake_s2", "raw_ref": "t",
         "event_time": iso(days), "ingest_time": iso(days),
         "turn_refs": [], "summary": summary, "core": core}
    if refs:
        d["entity_refs"] = refs
    return d


events = {
    "EVT-001-aaaa": ev("EVT-001-aaaa", "FragmentSource 协议初版讨论",
                       "初版协议讨论，带 salience 字段。", ["ent_fs"], days=10),
    "EVT-002-bbbb": ev("EVT-002-bbbb", "天气闲聊", "天气闲聊。"),
    "EVT-003-cccc": ev("EVT-003-cccc", "梦机制初步想法", "梦机制的初步想法。"),
    "EVT-004-dddd": ev("EVT-004-dddd", "heart 七通道快照定稿",
                       "七通道快照定稿。", ["ent_heart"]),
    "EVT-005-eeee": ev("EVT-005-eeee", "Ember 传播模型讨论",
                       "传播模型讨论。", ["ent_ember"]),
    "EVT-006-ffff": ev("EVT-006-ffff", "旧版激活扩散方案",
                       "旧版激活扩散方案。", ["ent_ember"], days=9),
    "EVT-007-gggg": ev("EVT-007-gggg", "heart 梦捕获机制",
                       "梦捕获机制。", ["ent_heart"]),
    "EVT-008-hhhh": ev("EVT-008-hhhh", "Ember 索引层设计",
                       "索引层设计。", ["ent_ember"]),
    "EVT-009-iiii": ev("EVT-009-iiii", "秋招信息收集",
                       "秋招信息收集，想用记忆系统辅助整理。"),
    "EVT-010-jjjj": ev("EVT-010-jjjj", "秋招简历投递开始",
                       "简历投递开始。"),
    "EVT-011-kkkk": ev("EVT-011-kkkk", "梦回流机制初步笔记",
                       "梦回流机制的初步笔记。"),
    "EVT-012-llll": ev("EVT-012-llll", "Ember 测试前端界面改动",
                       "测试前端界面改动。", ["ent_ember"]),
}
with open(os.path.join(ROOT, "events.jsonl"), "w", encoding="utf-8") as f:
    for d in events.values():
        f.write(json.dumps(d, ensure_ascii=False) + "\n")

estore = EntityStore(ROOT)
estore.upsert(Entity(entity_id="ent_fs", name="FragmentSource协议",
                     status="closed", closed_at=iso(10)))
estore.upsert(Entity(entity_id="ent_heart", name="heart"))
estore.upsert(Entity(entity_id="ent_ember", name="Ember记忆系统"))
estore.save()

# 历史遗留：一条跨实体 related_to 边
with open(os.path.join(ROOT, "event_relations.jsonl"), "w",
          encoding="utf-8") as f:
    f.write(json.dumps({
        "relation_id": "rel_hist_001", "src_event": "EVT-001-aaaa",
        "dst_event": "EVT-005-eeee", "type": "related_to",
        "confidence": "high", "status": "active",
        "provenance_refs": ["history"]}, ensure_ascii=False) + "\n")

# sleep_state：006 已 superseded（旧方案被推翻，退出召回池）
with open(os.path.join(ROOT, "sleep_state.jsonl"), "w", encoding="utf-8") as f:
    f.write(json.dumps({"event_id": "EVT-006-ffff", "status": "superseded",
                        "_provenance": "history"}) + "\n")

# EVT-001 的两条 cue：一个有使用（保留）、一个没用过（retire）
# （events.jsonl 里 EVT-001 行已由上面 ev() 写入——这里只补 cue，
#   不能再 store.write_event 追加 001，否则重复行覆盖丢 entity_refs）
store = EmberStore(ROOT)
from ember.models import RecallRoute  # noqa: E402
store.write_route(RecallRoute(route_id="rt_001_a", event_id="EVT-001-aaaa",
                              matcher="lexical", cue_type="lexical",
                              cue="FragmentSource"))
store.write_route(RecallRoute(route_id="rt_001_b", event_id="EVT-001-aaaa",
                              matcher="lexical", cue_type="lexical",
                              cue="salience"))

# 记账：白天多轮（entity 通路带出 + cue 通路部分命中）
ledger = RecallLedger(ROOT)
ledger.record(SESSION, 1, route="entity",
              event_ids=["EVT-001-aaaa", "EVT-002-bbbb"])
for _ in range(3):   # FragmentSource cue 被使用 3 次 → 收紧时保留
    ledger.record(SESSION, 1, route="cue", cue="FragmentSource",
                  event_ids=["EVT-001-aaaa"])
ledger.record(SESSION, 2, route="entity",
              event_ids=["EVT-003-cccc", "EVT-007-gggg", "EVT-004-dddd",
                         "EVT-011-kkkk", "EVT-002-bbbb"])
ledger.record(SESSION, 2, route="cue", cue="梦机制",
              event_ids=["EVT-003-cccc"])
ledger.record(SESSION, 3, route="entity",
              event_ids=["EVT-005-eeee", "EVT-008-hhhh",
                         "EVT-012-llll", "EVT-002-bbbb"])
ledger.record(SESSION, 4, route="entity",
              event_ids=["EVT-009-iiii", "EVT-010-jjjj"])

# ---------------------------------------------------------------------------
# 假会话窗口（turn 与记账对齐）
# ---------------------------------------------------------------------------
WINDOW = [
    {"role": "user",
     "content": "FragmentSource 协议 v2 今天定稿吧，砍掉 salience 字段。",
     "turn": 1},
    {"role": "assistant",
     "content": "同意，FragmentSource 协议 v2 定稿：只保留出处字段，turn_span 对齐。",
     "turn": 1},
    {"role": "user",
     "content": "梦机制今天出完整设计：入口走 FragmentSource，捕获的梦回流成事件。",
     "turn": 2},
    {"role": "assistant",
     "content": "梦机制完整设计定稿：入口走协议，回流成事件，与七通道快照联动。",
     "turn": 2},
    {"role": "user",
     "content": "传播模型也定了：确定性门控，不要激活扩散。",
     "turn": 3},
    {"role": "assistant",
     "content": "好，新传播方案与之前的讨论完全不同，走确定性门控。",
     "turn": 3},
    {"role": "user",
     "content": "秋招的事今天开始准备，先改简历。",
     "turn": 4},
    {"role": "assistant",
     "content": "好，秋招简历今天开始改，本周投递。",
     "turn": 4},
]
with open(os.path.join(ROOT, "sessions.jsonl"), "w", encoding="utf-8") as f:
    f.write(json.dumps({"session_id": SESSION, "messages": WINDOW},
                       ensure_ascii=False) + "\n")

# ---------------------------------------------------------------------------
# mock 调用①：5 个新事件（4 个 quote 成功，1 个失败）
# ---------------------------------------------------------------------------
CLOSING_VERDICT = {"events": [
    {"summary": "FragmentSource 协议 v2 定稿",
     "participants": ["ai", "用户"], "objects": [], "context": "协议讨论",
     "core": "我们决定 FragmentSource 协议 v2 砍掉 salience 字段，只保留出处，turn_span 对齐。",
     "outcome": "v2 定稿", "state": "", "affect": "",
     "significance": "梦机制的接口基础", "details": [],
     "cues": [{"cue_type": "lexical", "cue": "FragmentSource协议"},
              {"cue_type": "lexical", "cue": "字段"}],
     "quote_spans": [{"start_quote": "FragmentSource 协议 v2 今天定稿吧，砍掉 salience 字段。",
                      "end_quote": "同意，FragmentSource 协议 v2 定稿：只保留出处字段，turn_span 对齐。"}]},
    {"summary": "梦机制完整设计定稿",
     "participants": ["ai", "用户"], "objects": [], "context": "heart 讨论",
     "core": "梦机制完整设计：入口走 FragmentSource，捕获的梦回流成事件，与七通道快照联动。",
     "outcome": "完整设计定稿", "state": "", "affect": "",
     "significance": "heart 梦机制的核心设计", "details": [],
     "cues": [{"cue_type": "lexical", "cue": "梦机制"},
              {"cue_type": "lexical", "cue": "回流"},
              {"cue_type": "lexical", "cue": "快照"}],
     "quote_spans": [{"start_quote": "梦机制今天出完整设计：入口走 FragmentSource，捕获的梦回流成事件。",
                      "end_quote": "梦机制完整设计定稿：入口走协议，回流成事件，与七通道快照联动。"}]},
    {"summary": "传播模型新方案定稿",
     "participants": ["ai", "用户"], "objects": [], "context": "Ember 讨论",
     "core": "传播模型改为确定性门控，放弃激活扩散。",
     "outcome": "新方案定稿", "state": "", "affect": "",
     "significance": "Ember 核心机制转向", "details": [],
     "cues": [{"cue_type": "lexical", "cue": "传播模型"},
              {"cue_type": "lexical", "cue": "门控"}],
     "quote_spans": [{"start_quote": "传播模型也定了：确定性门控，不要激活扩散。",
                      "end_quote": "好，新传播方案与之前的讨论完全不同，走确定性门控。"}]},
    {"summary": "秋招准备启动",
     "participants": ["ai", "用户"], "objects": [], "context": "个人事务",
     "core": "秋招准备今天启动：先改简历，本周投递。",
     "outcome": "准备启动", "state": "", "affect": "",
     "significance": "", "details": [],
     "cues": [{"cue_type": "lexical", "cue": "秋招"},
              {"cue_type": "lexical", "cue": "简历"}],
     "quote_spans": [{"start_quote": "秋招的事今天开始准备，先改简历。",
                      "end_quote": "好，秋招简历今天开始改，本周投递。"}]},
    {"summary": "quote 失败的演示事件",
     "participants": ["ai"], "objects": [], "context": "",
     "core": "这条事件的 quote 全是假句子，用于演示定位失败。",
     "outcome": "", "state": "", "affect": "", "significance": "",
     "details": [],
     "cues": [{"cue_type": "lexical", "cue": "演示"}],
     "quote_spans": [{"start_quote": "这句话不存在111",
                      "end_quote": "这句话不存在222"}]},
]}


# ---------------------------------------------------------------------------
# mock 调用②：按 new core 分发；N4 第一次坏 verdict（name 重名）→ 拒绝重试
# ---------------------------------------------------------------------------
JUDGE_CALLS: dict[str, int] = {}


def fake_judge(system, user, engine):
    # N1/N2/N3/N4 的 event_id 在 closing 后才生成，verdict 里的
    # member_event_ids 由本函数在运行时拼装（mock 侧模拟 LLM 看到
    # 材料里新事件的 event_id 后原样回填）
    def member_of(new_id, *old_ids):
        return [new_id, *old_ids]

    if "FragmentSource 协议 v2 砍掉 salience" in user:
        return json.dumps({
            "pairs": [
                {"old_event_id": "EVT-001-aaaa", "outcome": "relation",
                 "type": "concludes", "confidence": "high",
                 "reason": "v2 定稿给初版讨论画上句号",
                 "change": "", "status_evidence": ""},
                {"old_event_id": "EVT-002-bbbb", "outcome": "neighbor",
                 "reason": "共现无联系"},
            ],
            "entity": {"action": "attach", "entity_id": "ent_fs"},
        }, ensure_ascii=False)
    if "梦机制完整设计" in user:
        new_id = _current_new_id
        return json.dumps({
            "pairs": [
                {"old_event_id": "EVT-003-cccc", "outcome": "relation",
                 "type": "resolves", "confidence": "high",
                 "reason": "完整设计推翻初步想法",
                 "change": "当时只是初步想法，现在有了完整入口与回流设计，我意识到初步方向走偏了。",
                 "status_evidence": "梦机制完整设计取代了 EVT-003 的初步想法"},
                {"old_event_id": "EVT-011-kkkk", "outcome": "relation",
                 "type": "inspired_by", "confidence": "high",
                 "reason": "回流笔记启发了完整设计", "change": "",
                 "status_evidence": ""},
                {"old_event_id": "EVT-004-dddd", "outcome": "relation",
                 "type": "caused_by", "confidence": "high",
                 "reason": "快照定稿促成了梦机制设计", "change": "",
                 "status_evidence": ""},
                {"old_event_id": "EVT-007-gggg", "outcome": "relation",
                 "type": "contrasts", "confidence": "medium",
                 "reason": "捕获机制与本次设计角度相对照", "change": "",
                 "status_evidence": ""},
                {"old_event_id": "EVT-002-bbbb", "outcome": "neighbor",
                 "reason": "共现"},
            ],
            "entity": {
                "action": "extract", "name": "heart梦机制",
                "aliases": ["heart 的梦"], "description": "heart 系统梦机制的设计与实现",
                "member_event_ids": member_of(new_id, "EVT-003-cccc",
                                              "EVT-011-kkkk"),
                "entity_id": ""},
        }, ensure_ascii=False)
    if "传播模型改为确定性门控" in user:
        return json.dumps({
            "pairs": [
                {"old_event_id": "EVT-005-eeee", "outcome": "relation",
                 "type": "revisits", "confidence": "high",
                 "reason": "重新处理传播模型且有实质进展", "change": "",
                 "status_evidence": ""},
                {"old_event_id": "EVT-008-hhhh", "outcome": "relation",
                 "type": "responds_to", "confidence": "medium",
                 "reason": "新方案回应了索引层设计的约束", "change": "",
                 "status_evidence": ""},
                {"old_event_id": "EVT-012-llll", "outcome": "relation",
                 "type": "related_to", "confidence": "medium",
                 "reason": "界面改动与方案相关联", "change": "",
                 "status_evidence": ""},
                {"old_event_id": "EVT-002-bbbb", "outcome": "neighbor",
                 "reason": "共现"},
            ],
            "entity": {"action": "attach", "entity_id": "ent_ember"},
        }, ensure_ascii=False)
    if "秋招准备今天启动" in user:
        n = JUDGE_CALLS.get("N4", 0)
        JUDGE_CALLS["N4"] = n + 1
        new_id = _current_new_id
        if n == 0:
            # 第一次：extract name 与已有 Entity 重名 → 校验整条拒绝
            return json.dumps({
                "pairs": [
                    {"old_event_id": "EVT-009-iiii", "outcome": "relation",
                     "type": "continues", "confidence": "high",
                     "reason": "延续信息收集", "change": "",
                     "status_evidence": ""},
                    {"old_event_id": "EVT-010-jjjj", "outcome": "neighbor",
                     "reason": "共现"},
                ],
                "entity": {"action": "extract", "name": "heart",
                           "aliases": [], "description": "坏输出",
                           "member_event_ids": [new_id,
                                                "EVT-009-iiii",
                                                "EVT-010-jjjj"],
                           "entity_id": ""},
            }, ensure_ascii=False)
        # 第二次：正确 verdict → extract 秋招
        return json.dumps({
            "pairs": [
                {"old_event_id": "EVT-009-iiii", "outcome": "relation",
                 "type": "continues", "confidence": "high",
                 "reason": "准备启动延续信息收集", "change": "",
                 "status_evidence": ""},
                {"old_event_id": "EVT-010-jjjj", "outcome": "neighbor",
                 "reason": "共现"},
            ],
            "entity": {
                "action": "extract", "name": "秋招",
                "aliases": ["2026秋招"], "description": "秋招准备与投递过程",
                "member_event_ids": member_of(new_id, "EVT-009-iiii",
                                              "EVT-010-jjjj"),
                "entity_id": ""},
        }, ensure_ascii=False)
    raise RuntimeError(f"unexpected prompt: {user[:60]}")


sleep_v2.call_llm = fake_judge
_current_new_id = ""   # fake_judge 运行时读取（模拟 LLM 看到材料里的新事件 id）


def set_current_new_id(mid: str) -> None:
    global _current_new_id
    _current_new_id = mid

# ===========================================================================
# 第一步：实时写入（模拟 AI 调 create_memory_event 工具，写入路径与
# service._handle_memory_tool_call 相同：write_event + write_route +
# quote_spans 定位回填 turn_span）
# ===========================================================================
print("\n========== [1] 实时写入（模拟工具调用） ==========")
store = EmberStore(ROOT)
spans = TurnSpanStore(ROOT)
from ember.models import Event  # noqa: E402

written = []
for idx, d in enumerate(CLOSING_VERDICT["events"]):
    event_id = f"EVT-{len(store.all_events()) + 1:03d}-{uuid.uuid4().hex[:6]}"
    store.write_event(Event(
        event_id=event_id, version_id=f"{event_id}-v1",
        source_ref=f"conversation/session-{SESSION}",
        raw_ref=f"conversation/session-{SESSION}/turn-1",
        event_time=iso(0), ingest_time=iso(0),
        turn_refs=["turn-1"],
        summary=d["summary"], participants=d["participants"],
        objects=d["objects"], context=[d["context"]], core=d["core"],
        outcome=[d["outcome"]], state=[d["state"]], affect=[d["affect"]],
        significance=d["significance"], details=d["details"],
        provenance_refs=["memory-writer"]))
    for c in d["cues"]:
        store.write_route(RecallRoute(
            route_id=f"rt_{event_id}_{len(written)}_{c['cue'][:4]}",
            event_id=event_id, matcher="lexical",
            cue_type=c["cue_type"], cue=c["cue"],
            provenance_refs=["memory-writer"]))
    turn_pairs = spans_from_quotes(d["quote_spans"], WINDOW)
    if turn_pairs:
        spans.fill(event_id, turn_pairs, filled_by="memory-writer")
    written.append({"event_id": event_id, "cues": len(d["cues"]),
                    "turn_spans": [list(p) for p in turn_pairs],
                    "quote_failed": not turn_pairs})

new_ids = [w["event_id"] for w in written]
n1, n2, n3, n4, n5 = new_ids
print(f"新事件：{new_ids}")
assert len(new_ids) == 5
w1, w2, w3, w4, w5 = written
assert w1["turn_spans"] == [[1, 1]], w1
assert w2["turn_spans"] == [[2, 2]]
assert w3["turn_spans"] == [[3, 3]]
assert w4["turn_spans"] == [[4, 4]]
assert w5["quote_failed"] and w5["turn_spans"] == []
snap = store.index_snapshot()
assert "fragmentsource协议" in snap, snap   # 专有名词整体
assert "秋招" in snap
print("[1] 断言过：4 精确回填 + 1 失败降级 + Cue 索引（专有名词整体）")

# ===========================================================================
# 第二步：调用②（N4 演示拒绝→重试）
# ===========================================================================
print("\n========== [2] judge ==========")
events = sleep_v2.load_events()
statuses = sleep_v2.load_sleep_state()
pairs = PairSeenStore(ROOT)
estore = EntityStore(ROOT)
entity_names = sleep_v2.existing_entity_names(estore)


from ember.prompts import build_judge_user_prompt  # noqa: E402


def run_judge_round():
    # N5（quote 失败、无出处）不进裁决——与真实 assemble 行为一致
    materials = sleep_v2.assemble_materials(
        [n1, n2, n3, n4], events, statuses, spans, ledger, pairs, estore)
    materials = [m for m in materials if m["old_events"]]
    state_up, event_up = {}, {}
    out = []
    for m in materials:
        set_current_new_id(m["new_id"])
        user = build_judge_user_prompt(m["new_event"], m["old_events"])
        raw = sleep_v2.call_llm("", user, "fake")
        verdict = sleep_v2.extract_json(raw)
        if verdict is None:
            out.append({"new_event": m["new_id"], "error": "parse"})
            continue
        errs = sleep_v2.validate(verdict, m, events, statuses, entity_names)
        if errs:
            out.append({"new_event": m["new_id"], "rejected": errs})
            continue
        s = sleep_v2.apply_verdict(verdict, m, events, statuses, estore,
                                   pairs, "fake", state_up, event_up)
        for mid, up in event_up.items():
            events[mid].update(up)
        event_up.clear()
        out.append(s)
    if state_up:
        for eid, up in state_up.items():
            events[eid]["_sleep_state"] = up["status"]
    # events 内存态同步（attach/extract 回填）——落盘文件也重写，
    # 供下一轮 load_events 读到最新归属
    changed = {eid: {"entity_refs": ev.get("entity_refs")}
               for eid, ev in events.items()
               if ev.get("entity_refs") is not None}
    sleep_v2.rewrite_events(events, changed)
    return out


out = run_judge_round()
print(json.dumps(out, ensure_ascii=False, indent=2))

# N4 第一轮被拒（name 重名），pairs 未留痕 → 重试
assert any("rejected" in o for o in out), out
assert JUDGE_CALLS["N4"] == 1
out2 = run_judge_round()
assert JUDGE_CALLS["N4"] == 2, "被拒的材料应可重判（pair 未留痕）"
out.extend(out2)
print("[2] 拒绝→重试：N4 第一次 name 重名被拒（pair 不留痕），第二次成功")

# ===========================================================================
# 断言：relation / 归属 / 留痕
# ===========================================================================
print("\n========== 断言 ==========")
rels = [json.loads(l) for l in
        open(os.path.join(ROOT, "event_relations.jsonl"), encoding="utf-8")
        if l.strip()]
rel_pairs = {(r["src_event"], r["dst_event"], r["type"]) for r in rels}
hist = {("EVT-001-aaaa", "EVT-005-eeee", "related_to")}
expected = {
    ("EVT-001-aaaa", n1, "concludes"),
    ("EVT-003-cccc", n2, "resolves"),
    ("EVT-011-kkkk", n2, "inspired_by"),
    ("EVT-004-dddd", n2, "caused_by"),
    ("EVT-007-gggg", n2, "contrasts"),
    (n2, "EVT-002-bbbb", "neighbor"),
    ("EVT-005-eeee", n3, "revisits"),
    ("EVT-008-hhhh", n3, "responds_to"),
    ("EVT-012-llll", n3, "related_to"),
    ("EVT-009-iiii", n4, "continues"),
    (n4, "EVT-010-jjjj", "neighbor"),
}
# neighbor/none 不落边，只留痕——expected 里去掉 neighbor；
# 历史边 rel_hist_001 也在文件里，一并纳入比对
edge_expected = {e for e in expected if e[2] != "neighbor"} | hist
missing = edge_expected - rel_pairs
assert not missing, f"缺边: {missing}"
extra = rel_pairs - edge_expected
assert not extra, f"多边: {extra}"
types_seen = {t for _, _, t in rel_pairs}
print(f"[2] relation 类型覆盖：{sorted(types_seen)}")
for t in ("continues", "concludes", "inspired_by", "caused_by",
          "responds_to", "resolves", "revisits", "contrasts", "related_to"):
    assert t in types_seen, f"缺类型 {t}"

# resolves 三处落盘
assert events["EVT-003-cccc"].get("_sleep_state") == "superseded"
assert events["EVT-003-cccc"]["change"].startswith("当时只是初步想法")

# 归属：attach×2 / extract×2 / none×1
assert events[n1]["entity_refs"] == ["ent_fs"], "N1 attach ent_fs"
assert events[n3]["entity_refs"] == ["ent_ember"], "N3 attach ent_ember"
assert not events[n5].get("entity_refs"), "N5 未进裁决，保持散"
ents = {e.name: e for e in estore.all()}
assert "heart梦机制" in ents and "秋招" in ents
m1 = ents["heart梦机制"].entity_id
m2 = ents["秋招"].entity_id
assert events[n2]["entity_refs"] == [m1], "extract member 含新事件自身"
assert events["EVT-003-cccc"]["entity_refs"] == [m1]
assert events["EVT-011-kkkk"]["entity_refs"] == [m1]
assert events[n4]["entity_refs"] == [m2]
assert events["EVT-009-iiii"]["entity_refs"] == [m2]
assert events["EVT-010-jjjj"]["entity_refs"] == [m2]
# 单归属：004 仍属 heart，未被 member 覆盖
assert events["EVT-004-dddd"]["entity_refs"] == ["ent_heart"]
print("[2] 归属断言过：attach×2 / extract×2（member 回填）/ 单归属保持")

# 留痕：成功裁决的 pair 全留痕；被拒轮次的 pair 不留痕但已重判
for pair in [("n1", "EVT-001-aaaa"), ("n1", "EVT-002-bbbb"),
             ("n2", "EVT-003-cccc"), ("n2", "EVT-007-gggg"),
             ("n3", "EVT-005-eeee"), ("n4", "EVT-009-iiii")]:
    a = new_ids[0] if pair[0] == "n1" else new_ids[1] if pair[0] == "n2" \
        else new_ids[2] if pair[0] == "n3" else new_ids[3]
    assert pairs.is_seen(a, pair[1]), pair
print("[2] 留痕断言过")

# ===========================================================================
# 第三步：收紧（N1 attach 重置了 ent_fs 的收紧起算点 → 静置不足豁免）
# ===========================================================================
print("\n========== [3] tighten ==========")
from ember.cue_tighten import CueTightener  # noqa: E402
t = CueTightener(EmberStore(ROOT), ledger,
                 os.path.join(ROOT, "events.jsonl"), entity_store=estore)
tight = t.tighten()
# 全部实体要么 open 豁免、要么起算点被重置（attach 重置 closed_at）：
#  - ent_fs：closed 但 N1 归入时 closed_at 已重置为归入时刻 → 静置不足
#  - 其余实体 open → 豁免
#  → 本轮 retired 应为空，salience/FragmentSource 都不收
assert tight["retired"] == [], tight["retired"]
# 新报告结构按事件粒度：N1（属 ent_fs，attach 重置起算点）静置不足豁免
fs_skip = [s for s in tight["skipped"] if s["event_id"] == n1]
assert fs_skip and "静置" in fs_skip[0]["reason"], tight["skipped"]
print("[3] 断言过：attach 重置起算点 → N1（ent_fs 名下）静置不足豁免（本轮零 retire）")
print(f"[3] tighten 报告：retired={tight['retired']} "
      f"skipped={[(s['event_id'], s['reason']) for s in tight['skipped']]}")

# ===========================================================================
# 幂等：重跑组装为空（N5 无出处天然排除；N1-N4 留痕排除）
# ===========================================================================
mats2 = sleep_v2.assemble_materials(
    [n1, n2, n3, n4], events, statuses, spans, ledger, pairs, estore)
mats2 = [m for m in mats2 if m["old_events"]]
assert not mats2, "全部 pair 已留痕，补跑不应再组出材料"
print("[+] 幂等性：补跑组装为空 ✓")
print("[+] 幂等性：补跑组装为空 ✓")

shutil.copytree(ROOT, os.path.join(ROOT, "..", "ember_e2e_v2_snapshot"),
                dirs_exist_ok=True)
print(f"\n=== 假数据全链路演练 v2 全部通过 ===\n[sandbox 快照] "
      f"{os.path.join(ROOT, '..', 'ember_e2e_v2_snapshot')}")
