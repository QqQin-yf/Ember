# -*- coding: utf-8 -*-
"""Ember v3 — Sleep 调用②（v2，架构文档 §4.2 新形态，2026-10-09 实现）。

被谁调用：手工离线运行（python scripts/sleep_v2.py --engine glm）；
将来由会话收尾触发端点串联（§8.1 待做第 5 条）。

职责（§4.2）：
  第一步 候选组装（纯代码）：今晚新 Event 的 turn_span 区间 ∩ 白天召回记账
         的 turn → 召回带出的旧 Events 配成材料。排除 superseded 与判过
         "无"的留痕 pair。材料标注旧 Event 所属实体（归属判断需要）。
  第二步 裁决与归属（LLM，每个新 Event 一次调用）：
         pairs 逐条三分支（relation/neighbor/none）+ entity 归属动作
         （extract/attach/none）。prompt 定稿见 ember/prompts.py。
  落盘（确定性校验 §4.3 通过后）：
         - relation 边 → event_relations.jsonl（direction 固定 旧→新）
         - resolves → 三处落盘：resolves 边 + status=superseded（sleep_state）
           + change 补写（events.jsonl 本体字段）
         - extract → 新 Entity（open）+ member 回填 entity_refs（events.jsonl）
         - attach → 新 Event entity_refs 指向已有 Entity
         - 三种结局的 pair 全部留痕（pair_seen.jsonl，含 relation）

不做：旧 Cue 收紧（第三步，ember/cue_tighten.py 已有，独立调用）；
      Direction 挂载（§5.2 独立作业）。

铁律：
- 旧 sleep_v1.py（全库逐条评估）被本脚本取代——按"不留旁支"约定，
  本脚本测试通过后 sleep_v1.py 删除。
- 所有校验失败整条拒绝，不写库；单 Event 失败不影响其他 Event。
- 单归属：每个 Event 的 entity_refs 至多一个；member 只收散事件。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember.prompts import JUDGE_SYSTEM_PROMPT, build_judge_user_prompt  # noqa: E402
from ember.pair_seen import PairSeenStore  # noqa: E402
from ember.relation import EventRelation, RELATION_TYPES  # noqa: E402
from ember.entity import Entity, EntityStore  # noqa: E402
from ember.turn_span import TurnSpanStore  # noqa: E402
from ember.recall_ledger import RecallLedger  # noqa: E402

DATA = os.path.join(BASE, "data_real")
EVENTS_FILE = os.path.join(DATA, "events.jsonl")
SLEEP_STATE = os.path.join(DATA, "sleep_state.jsonl")
RELATIONS_FILE = os.path.join(DATA, "event_relations.jsonl")

ALLOWED_OUTCOMES = {"relation", "neighbor", "none"}
ALLOWED_ACTIONS = {"extract", "attach", "none"}
ALLOWED_CONF = {"high", "medium", "low"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------

def load_events() -> dict[str, dict]:
    """events.jsonl 全量加载（后行覆盖前行，与 store 加载语义一致）。"""
    events: dict[str, dict] = {}
    with open(EVENTS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            events[d["event_id"]] = d
    return events


def load_sleep_state() -> dict[str, str]:
    """event_id -> status（读取现有判定，供 superseded 排除与落盘合并）。"""
    out: dict[str, str] = {}
    if not os.path.exists(SLEEP_STATE):
        return out
    with open(SLEEP_STATE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            eid = d.get("event_id") or ""
            if eid:
                out[eid] = d.get("status") or "valid"
    return out


def existing_entity_names(estore: EntityStore) -> set[str]:
    """已有 Entity 的 name + aliases（小写），开户查重用。"""
    names: set[str] = set()
    for e in estore.all():
        names.add(e.name.strip().lower())
        names.update(a.strip().lower() for a in e.aliases)
    return names


# ---------------------------------------------------------------------------
# 第一步：候选组装（纯代码）
# ---------------------------------------------------------------------------

def _session_of(event: dict) -> str | None:
    """从 Event 的 source_ref（conversation/session-{id}）解析所属 session。"""
    m = re.match(r"conversation/session-([^/]+)", event.get("source_ref") or "")
    return m.group(1) if m else None


def assemble_materials(new_event_ids: list[str], events: dict[str, dict],
                       statuses: dict[str, str], spans: TurnSpanStore,
                       ledger: RecallLedger, pairs: PairSeenStore,
                       estore: EntityStore) -> list[dict]:
    """每个新 Event 组一份材料。

    旧 Events 来源 = 召回记账（该新 Event 的 turn_span 区间 ∩ 记账 turn
    带出的 event）。排除：superseded/outdated 旧事件、判过的留痕 pair。
    记账查询按 Event 所属 session（source_ref 解析）精确匹配——跨 session
    的同号轮次不可混同；解析不出 session 的 Event 跳过并在 stderr 说明。
    旧 Events 为空的新 Event 跳过（无可裁决对象）。
    """
    # event_id -> 实体名（材料标注用）
    ev_entity: dict[str, str] = {}
    for eid, ev in events.items():
        refs = ev.get("entity_refs") or []
        if refs:
            try:
                ent = estore.get(refs[0])
                ev_entity[eid] = ent.name
            except KeyError:
                pass

    materials = []
    for new_id in new_event_ids:
        ev = events.get(new_id)
        if ev is None:
            continue
        # dict 无属性，不能用 span_of_event（getattr 静默降级），直接按 id 查；
        # 未回填时用 turn_refs 锚点降级（与 span_of_event 语义一致）
        turns = spans.span_of(new_id)
        if not turns:
            anchor = [int(m.group(1)) for ref in (ev.get("turn_refs") or [])
                      for m in [re.search(r"(\d+)$", str(ref))] if m]
            turns = spans.span_of(new_id, anchor)
        if not turns:
            continue
        # 记账带出的旧 events（该新 Event 被召回的那几轮）；
        # 按所属 session 精确查（跨 session 的同号轮次不可混同）
        sess = _session_of(ev)
        if sess is None:
            print(f"  !! {new_id} source_ref 无法解析 session，跳过",
                  file=sys.stderr)
            continue
        keys = [(sess, t) for span in turns
                for t in range(span[0], span[1] + 1)]
        brought = ledger.events_recalled_in_turns(keys)
        old_ids = []
        for oid in brought:
            if oid == new_id:
                continue
            if statuses.get(oid, "valid") not in ("valid", ""):
                continue          # superseded/outdated 不进材料
            if pairs.is_seen(new_id, oid):
                continue          # 判过"无"，永不再判
            old_ids.append(oid)

        materials.append({
            "new_id": new_id,
            "new_event": {"event_id": new_id,
                          "core": ev.get("core") or "",
                          "summary": ev.get("summary", "")},
            "old_events": [{"event_id": oid,
                            "entity_name": ev_entity.get(oid, ""),
                            "entity_id": (events[oid].get("entity_refs") or [""])[0],
                            "core": events[oid].get("core") or "",
                            "summary": events[oid].get("summary", "")}
                           for oid in old_ids if oid in events],
        })
    return materials


# ---------------------------------------------------------------------------
# 第二步：LLM 调用与解析
# ---------------------------------------------------------------------------

def call_llm(system: str, user: str, engine: str) -> str:
    """engine=glm：智谱 OpenAI-compatible API（环境变量配 key/base/model）。
    engine=qwen3：docker exec 进 ombre-ollama（本地兜底）。"""
    if engine == "qwen3":
        import subprocess
        full = system + "\n\n" + user
        r = subprocess.run(
            ["docker", "exec", "-i", "ombre-ollama", "ollama", "run",
             "qwen3:1.7b", "/no_think"],
            input=full.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        return r.stdout.decode("utf-8", errors="replace")
    if engine == "glm":
        import urllib.request
        api_key = os.environ.get("ZHIPU_API_KEY", "")
        base = os.environ.get("EMBER_BASE_URL",
                              "https://open.bigmodel.cn/api/paas/v4")
        model = os.environ.get("EMBER_SLEEP_MODEL", "glm-4.7")
        body = json.dumps({
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.2,
        }).encode("utf-8")
        req = urllib.request.Request(
            base.rstrip("/") + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    raise ValueError(f"未知 engine: {engine}")


def extract_json(text: str) -> dict | None:
    """剥 ANSI/思考标记，取第一个平衡 JSON 对象（qwen3 工程坑，沿用 v1 修法）。"""
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)
    text = re.sub(r"Thinking\.\.\.|done thinking\.", "", text)

    def try_parse(seg: str) -> dict | None:
        try:
            return json.loads(seg)
        except json.JSONDecodeError:
            pass
        try:
            return json.loads(re.sub(r"[\r\n\t]+", " ", seg))
        except json.JSONDecodeError:
            return None

    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"' and not esc:
                in_str = not in_str
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    d = try_parse(text[start:i + 1])
                    if d is not None:
                        return d
                    break
        start = text.find("{", start + 1)
    return None


# ---------------------------------------------------------------------------
# 确定性校验（§4.3）
# ---------------------------------------------------------------------------

def validate(verdict: dict, material: dict, events: dict[str, dict],
             statuses: dict[str, str], entity_names: set[str]) -> list[str]:
    errs: list[str] = []
    new_id = material["new_id"]
    old_ids = {o["event_id"] for o in material["old_events"]}

    pairs = verdict.get("pairs")
    if not isinstance(pairs, list):
        return ["pairs 缺失或不是列表"]

    seen_old = set()
    for p in pairs:
        oid = p.get("old_event_id")
        if oid not in old_ids:
            errs.append(f"pair 指向材料外事件: {oid}")
            continue
        if oid in seen_old:
            errs.append(f"pair 重复: {oid}")
        seen_old.add(oid)
        outcome = p.get("outcome")
        if outcome not in ALLOWED_OUTCOMES:
            errs.append(f"{oid} outcome 非法: {outcome}")
            continue
        if outcome == "relation":
            if p.get("type") not in RELATION_TYPES:
                errs.append(f"{oid} relation type 非法: {p.get('type')}")
            if p.get("confidence") not in ALLOWED_CONF:
                errs.append(f"{oid} confidence 非法: {p.get('confidence')}")
            if p.get("type") == "resolves":
                if not (p.get("change") or "").strip():
                    errs.append(f"{oid} resolves 缺 change")
                if not (p.get("status_evidence") or "").strip():
                    errs.append(f"{oid} resolves 缺 status_evidence")
        elif outcome == "neighbor" and p.get("type"):
            errs.append(f"{oid} neighbor 不应带 type")
        elif outcome == "none" and p.get("type"):
            errs.append(f"{oid} none 不应带 type")

    ent = verdict.get("entity") or {}
    action = ent.get("action", "none")
    if action not in ALLOWED_ACTIONS:
        errs.append(f"entity.action 非法: {action}")
        return errs

    if action == "extract":
        if not (ent.get("name") or "").strip():
            errs.append("extract 缺 name")
        elif ent["name"].strip().lower() in entity_names:
            errs.append(f"extract name 与已有 Entity 重复: {ent['name']}")
        members = ent.get("member_event_ids") or []
        if len(members) < 3:
            errs.append(f"extract member 不足 3: {len(members)}")
        for mid in members:
            if mid not in events:
                errs.append(f"member 不存在: {mid}")
                continue
            if events[mid].get("entity_refs"):
                errs.append(f"member 已有归属（散事件才可入）: {mid}")
    elif action == "attach":
        target = ent.get("entity_id") or ""
        # 目标必须是材料中某条旧 Event 归属的 Entity
        valid_targets = set()
        for o in material["old_events"]:
            refs = events.get(o["event_id"], {}).get("entity_refs") or []
            valid_targets.update(refs)
        if target not in valid_targets:
            errs.append(f"attach 目标不是材料内旧事件的实体: {target}")
        if events[new_id].get("entity_refs"):
            errs.append("attach 目标新事件已有归属（单归属）")
        # close 判断字段（可选）：bool 或 "true"/"false" 字符串，规范化
        close = ent.get("close", False)
        if isinstance(close, str):
            if close.lower() not in ("true", "false"):
                errs.append(f"close 非法: {close}")
            close = close.lower() == "true"
        elif not isinstance(close, bool):
            errs.append(f"close 非法: {close}")
            close = False
        if close and not (ent.get("close_reason") or "").strip():
            errs.append("close=true 缺 close_reason")
    return errs


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def append_jsonl(path: str, obj: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def rewrite_events(events: dict[str, dict], updates: dict[str, dict]) -> None:
    """events.jsonl 整文件重写（change 补写 / entity_refs 回填用）。

    只更新白名单字段（change / entity_refs），其余内容原样保留。
    """
    merged = {eid: dict(ev) for eid, ev in events.items()}
    for eid, up in updates.items():
        merged[eid].update(up)
    tmp = EVENTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for ev in merged.values():
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    os.replace(tmp, EVENTS_FILE)


def apply_verdict(verdict: dict, material: dict, events: dict[str, dict],
                  statuses: dict[str, str], estore: EntityStore,
                  pairs: PairSeenStore, engine: str,
                  state_updates: dict[str, dict],
                  event_updates: dict[str, dict]) -> dict:
    """校验通过的裁决落盘。返回动作摘要（报告用）。"""
    new_id = material["new_id"]
    prov = f"sleep-v2:{engine}"
    summary = {"new_event": new_id, "relations": [], "resolves": [],
               "entity_action": "none", "marked_pairs": 0}

    # --- pairs ---
    for p in verdict.get("pairs", []):
        oid = p["old_event_id"]
        outcome = p.get("outcome")
        # 判过的 pair 全部留痕（含 relation）——补跑与首跑等价，
        # 候选组装按留痕排除，重跑不会重复裁决/重复落边
        pairs.mark(new_id, oid, verdict=str(outcome), decided_by=prov)
        summary["marked_pairs"] += 1
        if outcome == "relation":
            rel = EventRelation(
                relation_id=f"rel_{uuid.uuid4().hex[:8]}",
                src_event=oid,           # 方向固定：旧 → 新
                dst_event=new_id,
                type=p["type"],
                confidence=p.get("confidence", "high"),
                evidence_refs=[f"{prov}:{p.get('reason', '')}"],
                provenance_refs=[prov],
            )
            append_jsonl(RELATIONS_FILE, rel.to_dict())
            summary["relations"].append(f"{oid}--{p['type']}-->{new_id}")
            if p["type"] == "resolves":
                # 三处落盘之二三：status + change
                state_updates[oid] = {"event_id": oid, "status": "superseded",
                                      "status_evidence": p.get("status_evidence", ""),
                                      "_provenance": prov}
                event_updates[oid] = {"change": p.get("change", "")}
                summary["resolves"].append(oid)

    # --- entity ---
    ent = verdict.get("entity") or {}
    action = ent.get("action", "none")
    if action == "extract":
        e = Entity(
            entity_id=f"ent_{uuid.uuid4().hex[:8]}",
            name=ent["name"].strip(),
            aliases=[a.strip() for a in (ent.get("aliases") or []) if a.strip()],
            provenance_refs=[prov],
        )
        estore.upsert(e)
        estore.save()
        members = list(dict.fromkeys(ent.get("member_event_ids") or []))
        for mid in members:
            refs = events[mid].get("entity_refs") or []
            if mid not in event_updates:
                event_updates[mid] = {}
            if not event_updates[mid].get("entity_refs") and not refs:
                event_updates[mid]["entity_refs"] = [e.entity_id]
        summary["entity_action"] = f"extract:{e.entity_id}({e.name}) x{len(members)}"
    elif action == "attach":
        target = ent["entity_id"]
        event_updates[new_id] = {"entity_refs": [target]}
        summary["entity_action"] = f"attach:{target}"
        # close 判断（momo 2026-10-09：attach 顺带判目标实体收尾）
        close = ent.get("close", False)
        if isinstance(close, str):
            close = close.lower() == "true"
        try:
            target_ent = estore.get(target)
        except KeyError:
            target_ent = None
        now = _now_iso()
        if target_ent is not None:
            if close:
                # AI 判定收尾 → close（open 直接关；已 closed 重置起算点）
                if target_ent.status == "open":
                    target_ent.close(now)
                    summary["attach_closed"] = target
                target_ent.closed_at = now
                estore.save()
                summary["attach_reset_closed_at"] = target_ent.closed_at
            elif target_ent.status == "closed":
                # 归入已 close 的实体且未判收尾 → 重置起算点（区域重新活跃）
                target_ent.closed_at = now
                estore.save()
                summary["attach_reset_closed_at"] = target_ent.closed_at
    return summary


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Sleep 调用②（v2，§4.2）")
    ap.add_argument("--engine", choices=["glm", "qwen3"], default="glm")
    ap.add_argument("--new", default="",
                    help="今晚新 Event 的 event_id（逗号分隔）；缺省=取 events.jsonl 末尾 N 条")
    ap.add_argument("--recent", type=int, default=1,
                    help="缺省新 Event 时取末尾几条（默认 1）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只组装材料打印，不调用 LLM 不落盘")
    args = ap.parse_args()

    from ember.env import load_env
    load_env(os.path.dirname(DATA))
    events = load_events()
    statuses = load_sleep_state()
    estore = EntityStore(DATA)
    spans = TurnSpanStore(DATA)
    ledger = RecallLedger(DATA)
    pairs = PairSeenStore(DATA)

    if args.new:
        new_ids = [x.strip() for x in args.new.split(",") if x.strip()]
    else:
        new_ids = list(events)[-args.recent:]

    materials = assemble_materials(new_ids, events, statuses, spans,
                                   ledger, pairs, estore)
    if args.dry_run:
        print(json.dumps(materials, ensure_ascii=False, indent=2))
        return
    materials = [m for m in materials if m["old_events"]]
    if not materials:
        print("没有可裁决的材料（无召回带出的旧事件）", file=sys.stderr)
        return

    entity_names = existing_entity_names(estore)
    state_updates: dict[str, dict] = {}
    event_updates: dict[str, dict] = {}
    all_event_updates: dict[str, dict] = {}   # 累计全部批次，收尾统一重写文件
    report = []

    for m in materials:
        print(f"裁决 {m['new_id']}（旧事件 {len(m['old_events'])} 条）...",
              file=sys.stderr)
        user = build_judge_user_prompt(m["new_event"], m["old_events"])
        raw = None
        for attempt in range(3):
            try:
                raw = call_llm(JUDGE_SYSTEM_PROMPT, user, args.engine)
                break
            except Exception as ex:
                print(f"  !! 第{attempt+1}次调用失败: {ex}", file=sys.stderr)
        if raw is None:
            print(f"!! {m['new_id']} 三次均失败，跳过", file=sys.stderr)
            continue
        verdict = extract_json(raw)
        if verdict is None:
            print(f"!! {m['new_id']} JSON 解析失败，跳过", file=sys.stderr)
            continue
        errs = validate(verdict, m, events, statuses, entity_names)
        if errs:
            print(f"!! {m['new_id']} 校验失败（整条拒绝）: {errs}", file=sys.stderr)
            continue
        # events 内存态同步（member 回填后同批后续校验能看到）
        summary = apply_verdict(verdict, m, events, statuses, estore,
                                pairs, args.engine, state_updates, event_updates)
        for mid, up in event_updates.items():
            events[mid].update(up)
            all_event_updates.setdefault(mid, {}).update(up)
        event_updates.clear()
        report.append(summary)

    # 统一落盘：events.jsonl 重写（change/entity_refs）+ sleep_state 合并
    if all_event_updates:
        rewrite_events(events, all_event_updates)
    if state_updates:
        existing_state: dict[str, dict] = {}
        if os.path.exists(SLEEP_STATE):
            with open(SLEEP_STATE, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            d = json.loads(line)
                            existing_state[d["event_id"]] = d
                        except json.JSONDecodeError:
                            pass
        for eid, up in state_updates.items():
            row = existing_state.get(eid, {})
            row.update(up)
            existing_state[eid] = row
        with open(SLEEP_STATE, "w", encoding="utf-8") as f:
            for row in existing_state.values():
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
