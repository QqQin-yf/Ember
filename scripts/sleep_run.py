# -*- coding: utf-8 -*-
"""Ember v3 — Sleep 入口（momo 2026-10-09 定稿的理想流程）。

流程（momo 拍板）：
  写入 = AI 调工具实时写（白天，已存在），写几次都行，不触发 sleep；
  Event 攒起来；用户手动点 sleep（运行本脚本）→ 维护全部跑完；
  每个 Event 哪个阶段出问题都能续跑。

职责：
  1. 自动捞出"攒着的、还没维护过"的 Event（查 sleep_progress 账本，
     没记录的即待处理；--new 可显式追加）
  2. 调用②裁决与归属（sleep_v2：一批材料一次调用，三分支+归属）
  3. 旧 Cue 收紧（CueTightener）
  4. 跑完的 Event 记入进度账本（sleep_progress.jsonl）；
     失败的不记 → 下次 sleep 自动续跑
  （Direction 划分/挂载不在此列：达阈值时打印提示，手动跑对应脚本）

engine：缺省从 executors.json 的 judge 项读（--engine 单次覆盖）。

失败语义（2026-10-09 定稿）：
  - 单 Event 裁决失败 → 该次结果丢弃、不记进度 → 下次 sleep 自动续跑；
  - 其他 Event 不受影响；
  - 收紧无数据依赖，照常执行。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import sleep_v2  # noqa: E402
from ember import executors  # noqa: E402
from ember.prompts import (  # noqa: E402
    build_judge_user_prompt, SUMMARY_SYSTEM_PROMPT, build_summary_user_prompt,
)
from ember.entity import EntityStore  # noqa: E402
from ember.turn_span import TurnSpanStore  # noqa: E402
from ember.recall_ledger import RecallLedger  # noqa: E402
from ember.pair_seen import PairSeenStore  # noqa: E402
from ember.cue_tighten import CueTightener  # noqa: E402
from ember.store import EmberStore  # noqa: E402

DATA = os.path.join(BASE, "data_real")
PROGRESS_FILE = os.path.join(DATA, "sleep_progress.jsonl")


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def load_progress() -> set[str]:
    """已维护过的 event_id 集合（append-only 账本，可重建）。"""
    done: set[str] = set()
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        d = json.loads(line)
                        if d.get("event_id"):
                            done.add(d["event_id"])
                    except json.JSONDecodeError:
                        continue
    return done


def mark_progress(event_id: str, engine: str, detail: dict) -> None:
    with open(PROGRESS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "event_id": event_id, "stage": "judged",
            "engine": engine, "recorded_at": _now_iso(),
            "detail": detail,
        }, ensure_ascii=False) + "\n")


def pending_events(events: dict[str, dict], done: set[str],
                   extra: list[str]) -> list[str]:
    """待维护 Event = 全库事件 - 已维护 - superseded/outdated + extra 指定。

    按写入顺序（event_id 序）排列， older first。
    """
    out = []
    for eid in sorted(events):
        if eid in done:
            continue
        out.append(eid)
    for eid in extra:
        if eid not in out:
            out.append(eid)
    return out


def main():
    ap = argparse.ArgumentParser(
        description="Sleep 入口：裁决攒着的 Event → 收紧（§4.2）")
    ap.add_argument("--engine", choices=["glm", "qwen3"], default=None,
                    help="单次覆盖；缺省读 executors.json 的 judge 项")
    ap.add_argument("--new", default="",
                    help="显式追加要裁决的 event_id（逗号分隔，即使已维护过）")
    ap.add_argument("--skip-tighten", action="store_true",
                    help="只跑裁决，跳过收紧")
    ap.add_argument("--dry-run", action="store_true",
                    help="只列出待维护 Event，不调用 LLM 不落盘")
    args = ap.parse_args()
    from ember import executors  # noqa: E402
    engine = executors.load_engine(BASE, "judge", args.engine)

    from ember.env import load_env
    load_env(os.path.dirname(DATA))
    events = sleep_v2.load_events()
    statuses = sleep_v2.load_sleep_state()
    done = load_progress()
    extra = [x.strip() for x in args.new.split(",") if x.strip()]
    pending = pending_events(events, done, extra)

    print(f"[sleep] 待维护 Event {len(pending)} 条"
          f"（账本已维护 {len(done)} 条）", file=sys.stderr)
    if args.dry_run:
        print(json.dumps({"pending": pending, "done_count": len(done)},
                         ensure_ascii=False, indent=2))
        return
    if not pending:
        print("没有攒着的新 Event，无事可做。", file=sys.stderr)

    estore = EntityStore(DATA)
    spans = TurnSpanStore(DATA)
    ledger = RecallLedger(DATA)
    pairs = PairSeenStore(DATA)
    entity_names = sleep_v2.existing_entity_names(estore)
    touched_entities: set[str] = set()   # 本轮被 extract/attach 的实体（summary 步用）

    # ---------- 调用②：裁决与归属（每 Event 一次调用） ----------
    # 进度语义（momo 2026-10-09）：每个 Event 独立记账、独立续跑——
    # 失败的 Event 在跑完后的报告里单独列出（failed 段），修复后用
    # --new <id> 从它的断点（裁决步）单独续跑，不影响其他 Event。
    judged: list[dict] = []
    failed: list[dict] = []
    if pending:
        materials = sleep_v2.assemble_materials(
            pending, events, statuses, spans, ledger, pairs, estore)
        # 无召回带出旧事件的新 Event：不是不裁，而是"无可裁对象"——
        # 也记进度（散事件靠 Cue/Relation 召回，sleep 对它无事可做），
        # 否则会永远留在 pending 里。
        no_material = [e for e in pending
                       if e not in {m["new_id"] for m in materials}]
        for eid in no_material:
            mark_progress(eid, engine, {"note": "无召回带出的旧事件，无可裁对象"})
            judged.append({"new_event": eid, "skipped": "无可裁对象（散事件）"})
        materials = [m for m in materials if m["old_events"]]

        for m in materials:
            print(f"[sleep] 裁决 {m['new_id']}（旧事件 {len(m['old_events'])}）...",
                  file=sys.stderr)
            user = build_judge_user_prompt(m["new_event"], m["old_events"])
            raw = None
            for attempt in range(3):
                try:
                    raw = sleep_v2.call_llm(sleep_v2.JUDGE_SYSTEM_PROMPT,
                                            user, engine)
                    break
                except Exception as ex:
                    print(f"  !! 第{attempt+1}次调用失败: {ex}", file=sys.stderr)
            verdict = sleep_v2.extract_json(raw) if raw else None
            if verdict is None:
                # 断点 = 裁决步：该次结果丢弃、不记进度，报告 failed 段列出
                failed.append({"event_id": m["new_id"],
                               "stage": "judge",
                               "error": "调用/解析失败（3 次重试均失败）"})
                continue
            errs = sleep_v2.validate(verdict, m, events, statuses,
                                     entity_names)
            if errs:
                # 断点 = 裁决步：该次结果丢弃、pair 不留痕（可重判），
                # 报告 failed 段列出，等人工修复后续跑
                failed.append({"event_id": m["new_id"],
                               "stage": "judge-validate",
                               "error": errs})
                continue
            state_up, event_up = {}, {}
            s = sleep_v2.apply_verdict(verdict, m, events, statuses, estore,
                                       pairs, engine, state_up,
                                       event_up)
            for mid, up in event_up.items():
                events[mid].update(up)
            if state_up:
                # superseded 落 sleep_state（追加，查询端后行覆盖）
                with open(sleep_v2.SLEEP_STATE, "a", encoding="utf-8") as f:
                    for eid, up in state_up.items():
                        f.write(json.dumps({"event_id": eid, **up},
                                           ensure_ascii=False) + "\n")
            if event_up:
                sleep_v2.rewrite_events(events, event_up)
                events.update({eid: {**events.get(eid, {}), **up}
                               for eid, up in event_up.items()})
            mark_progress(m["new_id"], engine, s)
            judged.append(s)
            # 归属动作收集（summary 步触发用）：该实体本轮被 extract/attach 过
            action = (verdict.get("entity") or {}).get("action", "none")
            if action == "extract":
                touched_entities.add(
                    (verdict.get("entity") or {}).get("entity_id_ref", ""))
            elif action == "attach":
                touched_entities.add((verdict.get("entity") or {}).get("entity_id", ""))
            # extract 的实体 id 是 apply_verdict 里新生成的（ent_xxx），
            # 从 apply 结果 summary 里取更可靠
            ea = s.get("entity_action", "")
            if ea.startswith("extract:"):
                touched_entities.discard("")
                touched_entities.add(ea.split(":")[1].split("(")[0])

    # ---------- Entity summary 重算（外包任务点，momo 2026-10-10 定稿） ----------
    # 触发：本轮裁决中发生过 extract/attach 的实体，逐个重算基本属性描述；
    # 旧 summary 作为参考一并给入；失败不影响其他实体（报告单独列出）。
    summary_results: list[dict] = []
    touched_entities = {e for e in touched_entities if e}
    if touched_entities:
        s_engine = executors.load_engine(BASE, "summary")
        s_user = build_summary_user_prompt
        for ent_id in sorted(touched_entities):
            try:
                ent = estore.get(ent_id)
            except KeyError:
                continue
            member_sums = [
                ev.get("summary", "") for eid, ev in events.items()
                if ent_id in (ev.get("entity_refs") or [])
                and ev.get("summary", "")]
            if not member_sums:
                continue   # 名下无事件，无素材可概括
            user = s_user(ent.name, ent.summary or "", member_sums)
            raw = None
            for attempt in range(3):
                try:
                    raw = executors.call_llm(SUMMARY_SYSTEM_PROMPT, user,
                                             s_engine, task="summary")
                    break
                except Exception as ex:
                    print(f"  !! summary 第{attempt+1}次调用失败"
                          f"（{ent_id}）: {ex}", file=sys.stderr)
            parsed = sleep_v2.extract_json(raw) if raw else None
            new_summary = (parsed or {}).get("summary", "")
            if not isinstance(new_summary, str) or not new_summary.strip():
                summary_results.append({"entity_id": ent_id,
                                        "status": "failed",
                                        "error": "调用/解析失败或空 summary"})
                continue
            ent.summary = new_summary.strip()
            ent.summary_at = _now_iso()
            estore.save()
            summary_results.append({"entity_id": ent_id,
                                    "status": "updated",
                                    "summary": ent.summary})

    # ---------- 收紧（无数据依赖，照常执行） ----------
    tighten = None
    if not args.skip_tighten:
        t = CueTightener(EmberStore(DATA), ledger,
                         os.path.join(DATA, "events.jsonl"),
                         entity_store=estore)
        tighten = t.tighten()

    report = {"engine": engine, "finished_at": _now_iso(),
              "judged": judged, "failed": failed,
              "summary": summary_results, "tighten": tighten,
              "progress_done": len(load_progress())}
    print(json.dumps(report, ensure_ascii=False, indent=2))

    # 失败汇总：醒目列出 + 各自的续跑命令
    if failed:
        print("\n===== 以下 Event 未完成维护（断点=裁决步），修复后各自续跑 =====",
              file=sys.stderr)
        for f in failed:
            print(f"  {f['event_id']}  [{f['stage']}]  "
                  f"{json.dumps(f['error'], ensure_ascii=False)}", file=sys.stderr)
        ids = ",".join(f["event_id"] for f in failed)
        print(f"  续跑命令：python scripts/sleep_run.py --engine {engine} "
              f"--new {ids}", file=sys.stderr)


if __name__ == "__main__":
    main()
