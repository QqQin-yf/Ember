# -*- coding: utf-8 -*-
"""Ember v3 — Direction 划分作业（§5.1，2026-10-09 实现）。

被谁调用：手工离线运行（python scripts/split_directions.py --entity <id>）；
pipeline.py 在 B1 之后按阈值自动触发。

职责（§5.1 定稿）：
  触发：Entity 下 Event 达 DIRECTION_THRESHOLD=8（baseline）。
  输入：实体名 + 该实体下全部事件摘要 + 潜在用户问法（prompt 声明）。
  输出：directions 描述列表（LLM 只给 description，id 由本脚本生成，
        格式 dir_<entity_short>_<n>，与既有手动 dir_ember_* 对齐）。
  落盘：entities.jsonl（EntityStore 整文件重写，追加 directions）。
  配套：划分后需重跑 scripts/build_direction_vectors.py 生成向量，
        运行时 DirectionMatcher 才能对该 Entity 聚焦（否则回退全区域，
        基础设施降级不引入新行为——本脚本打印提醒，不自动调 docker）。

不做：挂载（mount_directions.py 的活）；动态重构（v1 纪律）。
已有方向的 Entity 重复运行会追加而非覆盖——由调用方决定是否重跑。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember.prompts import SPLIT_SYSTEM_PROMPT, build_split_user_prompt  # noqa: E402
from ember.entity import EntityStore, Direction  # noqa: E402
from ember.recall import register_event_entity_refs  # noqa: E402

DATA = os.path.join(BASE, "data_real")
EVENTS_FILE = os.path.join(DATA, "events.jsonl")

DIRECTION_THRESHOLD = 8   # baseline（§5.1）


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
    out = []
    for eid, ev in events.items():
        refs = ev.get("entity_refs") or []
        if entity_id in refs or any(r.startswith(entity_id) for r in refs):
            out.append(eid)
    return out


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
        import urllib.request
        api_key = os.environ.get("ZHIPU_API_KEY", "")
        base = os.environ.get("EMBER_BASE_URL",
                              "https://open.bigmodel.cn/api/paas/v4")
        model = os.environ.get("EMBER_SPLIT_MODEL",
                               os.environ.get("EMBER_SLEEP_MODEL", "glm-4.7"))
        body = json.dumps({
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.3,
        }).encode("utf-8")
        req = urllib.request.Request(
            base.rstrip("/") + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    raise ValueError(f"未知 engine: {engine}")


def extract_json(text: str) -> dict | None:
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


def validate(verdict: dict) -> list[str]:
    errs = []
    dirs = verdict.get("directions")
    if not isinstance(dirs, list) or not dirs:
        return ["directions 缺失或为空"]
    for d in dirs:
        if not (d.get("description") or "").strip():
            errs.append("存在空 description")
    if not (verdict.get("reason") or "").strip():
        errs.append("缺 reason")
    return errs


def main():
    ap = argparse.ArgumentParser(description="Direction 划分（§5.1）")
    ap.add_argument("--engine", choices=["glm", "qwen3"], default="glm")
    ap.add_argument("--entity", required=True, help="目标 Entity 的 entity_id")
    ap.add_argument("--dry-run", action="store_true",
                    help="只打印将参与划分的事件清单")
    args = ap.parse_args()

    from ember.env import load_env
    load_env(os.path.dirname(DATA))
    events = load_events()
    estore = EntityStore(DATA)
    try:
        entity = estore.get(args.entity)
    except KeyError:
        raise SystemExit(f"Entity 不存在: {args.entity}")

    if entity.directions:
        print(f"Entity {entity.name} 已有 {len(entity.directions)} 个方向，"
              "v1 不做动态重构；如确认要重划，先手工清理 entities.jsonl。",
              file=sys.stderr)
        return

    member_ids = events_of_entity(events, args.entity)
    if len(member_ids) < DIRECTION_THRESHOLD:
        print(f"Entity {entity.name} 下 Event {len(member_ids)} 条 "
              f"< 阈值 {DIRECTION_THRESHOLD}，不划分（整区域召回即可）。",
              file=sys.stderr)
        return

    summaries = [f"{eid} {events[eid].get('summary', '')}" for eid in member_ids]
    if args.dry_run:
        print(json.dumps({"entity": args.entity,
                          "member_count": len(member_ids),
                          "events": summaries},
                         ensure_ascii=False, indent=2))
        return

    user = build_split_user_prompt(entity.name, summaries)
    raw = None
    for attempt in range(3):
        try:
            raw = call_llm(SPLIT_SYSTEM_PROMPT, user, args.engine)
            break
        except Exception as ex:
            print(f"!! 第{attempt+1}次调用失败: {ex}", file=sys.stderr)
    if raw is None:
        raise SystemExit("三次调用均失败")
    verdict = extract_json(raw)
    if verdict is None:
        raise SystemExit("JSON 解析失败")
    errs = validate(verdict)
    if errs:
        raise SystemExit(f"校验失败（整条拒绝）: {errs}")

    # id 代码生成：dir_<entity短名>_<序号>，与既有 dir_ember_* 对齐
    short = args.entity.replace("ent_", "") or "ent"
    start_n = len(entity.directions) + 1
    for i, d in enumerate(verdict["directions"]):
        entity.directions.append(Direction(
            direction_id=f"dir_{short}_{start_n + i}",
            description=d["description"].strip(),
        ))
    estore.save()
    print(json.dumps({
        "entity": args.entity,
        "added": [d.direction_id for d in entity.directions],
        "reason": verdict.get("reason", ""),
        "next": "python scripts/build_direction_vectors.py  # 重建向量文件",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
