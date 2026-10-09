# -*- coding: utf-8 -*-
"""Ember v3 — Direction 挂载作业（B2，架构文档 §5.2，2026-10-09 实现）。

被谁调用：手工离线运行（python scripts/mount_directions.py --engine glm）。
独立离线作业，不属于 Sleep 流水线（§4.4/§5.2）。

触发（§5.2）：Entity 下 Event 条数达阈值（DIRECTION_THRESHOLD=8，baseline）。
执行者：便宜 LLM / 本地模型（glm API 或本地 qwen3），与 Chat Model、Sleep
执行者均无关。prompt 定稿（momo 2026-10-08 批准）存 ember/prompts.py。

职责：
  输入：Event 的 core + 该 Entity 全部 Direction 的 description
  输出：direction_ids（可空，"没有贴切方向就不挂"，不发明输入没有的方向）
  落盘：events.jsonl 侧表 direction_refs（增量合并，不覆盖已有挂载）
  配套：挂载后 Direction 向量无需变更（向量来自 description，与挂载无关），
        但新方向划分后要重跑 build_direction_vectors.py。

不做：未达阈值的 Entity（整区域召回即可）；prompt 外的任何判断。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember.prompts import (  # noqa: E402
    MOUNT_SYSTEM_PROMPT, build_mount_user_prompt,
)
from ember.entity import EntityStore  # noqa: E402

DATA = os.path.join(BASE, "data_real")
EVENTS_FILE = os.path.join(DATA, "events.jsonl")

DIRECTION_THRESHOLD = 8   # baseline（§5.1）：Entity 下 Event 达 8 个才划分/挂载


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------

def load_events() -> dict[str, dict]:
    events: dict[str, dict] = {}
    with open(EVENTS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            events[d["event_id"]] = d
    return events


def events_of_entity(events: dict[str, dict], entity_id: str) -> list[str]:
    """entity_refs 指向该实体的全部 event（完整 id 优先，短前缀兼容）。"""
    out = []
    for eid, ev in events.items():
        refs = ev.get("entity_refs") or []
        if entity_id in refs:
            out.append(eid)
        elif any(r.startswith(entity_id) for r in refs):
            out.append(eid)
    return out


def rewrite_events(events: dict[str, dict], updates: dict[str, dict]) -> None:
    """events.jsonl 整文件重写；只更新白名单字段 direction_refs。"""
    merged = {eid: dict(ev) for eid, ev in events.items()}
    for eid, up in updates.items():
        merged[eid].update(up)
    tmp = EVENTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for ev in merged.values():
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    os.replace(tmp, EVENTS_FILE)


# ---------------------------------------------------------------------------
# LLM（与 sleep_v2 同构：glm / qwen3）
# ---------------------------------------------------------------------------

def call_llm(system: str, user: str, engine: str) -> str:
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
        api_key = os.environ.get("ZHIPU_API_KEY", "")
        base = os.environ.get("EMBER_BASE_URL",
                              "https://open.bigmodel.cn/api/paas/v4")
        model = os.environ.get("EMBER_MOUNT_MODEL",
                               os.environ.get("EMBER_SLEEP_MODEL", "glm-4.7"))
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
    """剥 ANSI/思考标记取第一个平衡 JSON（与 sleep_v2 同一修法）。"""
    import re
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
# 确定性校验（§4.3 同源纪律）
# ---------------------------------------------------------------------------

def validate(verdict: dict, valid_direction_ids: set[str]) -> list[str]:
    errs = []
    ids = verdict.get("direction_ids", [])
    if not isinstance(ids, list):
        return ["direction_ids 不是列表"]
    for d in ids:
        if d not in valid_direction_ids:
            errs.append(f"发明了输入里没有的方向: {d}")
    if not (verdict.get("reason") or "").strip():
        errs.append("缺 reason")
    return errs


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Direction 挂载作业（§5.2，B2）")
    ap.add_argument("--engine", choices=["glm", "qwen3"], default="glm")
    ap.add_argument("--entity", required=True,
                    help="目标 Entity 的 entity_id（如 ent_ember）")
    ap.add_argument("--event", default="",
                    help="只挂载指定 event_id（逗号分隔）；缺省=该实体下全部未挂载事件")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将挂载的事件清单，不调用 LLM 不落盘")
    args = ap.parse_args()

    from ember.env import load_env
    load_env(os.path.dirname(DATA))
    events = load_events()
    estore = EntityStore(DATA)
    try:
        entity = estore.get(args.entity)
    except KeyError:
        raise SystemExit(f"Entity 不存在: {args.entity}")

    directions = entity.directions
    if not directions:
        print(f"Entity {entity.name} 没有已划分的 Direction（未达阈值或未划分），"
              "整区域召回即可，无需挂载。", file=sys.stderr)
        return

    member_ids = events_of_entity(events, args.entity)
    if len(member_ids) < DIRECTION_THRESHOLD:
        print(f"Entity {entity.name} 下 Event {len(member_ids)} 条 "
              f"< 阈值 {DIRECTION_THRESHOLD}，不挂载（整区域召回即可）。",
              file=sys.stderr)
        return

    if args.event:
        targets = [x.strip() for x in args.event.split(",") if x.strip()]
    else:
        targets = [eid for eid in member_ids
                   if not events[eid].get("direction_refs")]

    if args.dry_run:
        print(json.dumps({
            "entity": args.entity, "member_count": len(member_ids),
            "directions": [d.direction_id for d in directions],
            "to_mount": targets,
        }, ensure_ascii=False, indent=2))
        return
    if not targets:
        print("全部事件已挂载，无事可做。", file=sys.stderr)
        return

    dir_lines = "\n".join(f"{d.direction_id}: {d.description}"
                          for d in directions)
    valid_ids = {d.direction_id for d in directions}
    updates: dict[str, dict] = {}
    report = []

    for eid in targets:
        ev = events[eid]
        core = ev.get("core") or ev.get("summary") or ""
        user = build_mount_user_prompt(dir_lines, eid, core)
        print(f"挂载 {eid} ...", file=sys.stderr)
        raw = None
        for attempt in range(3):
            try:
                raw = call_llm(MOUNT_SYSTEM_PROMPT, user, args.engine)
                break
            except Exception as ex:
                print(f"  !! 第{attempt+1}次调用失败: {ex}", file=sys.stderr)
        if raw is None:
            print(f"!! {eid} 三次均失败，跳过", file=sys.stderr)
            continue
        verdict = extract_json(raw)
        if verdict is None:
            print(f"!! {eid} JSON 解析失败，跳过", file=sys.stderr)
            continue
        errs = validate(verdict, valid_ids)
        if errs:
            print(f"!! {eid} 校验失败（整条拒绝）: {errs}", file=sys.stderr)
            continue
        ids = list(dict.fromkeys(verdict.get("direction_ids") or []))
        if ids:
            merged = list(dict.fromkeys(
                (ev.get("direction_refs") or []) + ids))
            updates[eid] = {"direction_refs": merged}
            events[eid]["direction_refs"] = merged   # 内存态同步
        report.append({"event_id": eid, "mounted": ids,
                       "reason": verdict.get("reason", "")})

    if updates:
        rewrite_events(events, updates)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
