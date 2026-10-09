# -*- coding: utf-8 -*-
"""实验：让 qwen3:1.7b 自行为 Ember Entity 划分 Direction（不泄露手动划分答案）。
只做实验，不改任何数据文件。
"""
import json
import re
import subprocess
import sys

BASE = r"D:\ruanjian\workbuddy项目文件夹\2026-09-30-17-06-35\ember-v3-mvp"
ENTITY = "ent_ember"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def load_entity_events():
    events = []
    with open(BASE + r"\data_real\events.jsonl", encoding="utf-8") as f:
        for line in f:
            e = json.loads(line)
            if ENTITY in (e.get("entity_refs") or []):
                events.append(e)
    return events


def build_prompt(events):
    lines = []
    for e in events:
        body = e.get("core") or e.get("summary") or ""
        lines.append(f"[{e['event_id']}] {body}")
    corpus = "\n\n".join(lines)

    prompt = """/no_think
下面是同一个实体（Ember，一个记忆系统）的全部经历记录。将来用户会从不同角度提起这个实体，每个角度就是一条「方向」（Direction）：它决定一句话进来时从哪里切入这些经历。

你的任务：
1. 阅读全部经历，自行归纳出若干条方向。方向数量由内容决定，不要人为凑数。
2. 每条方向写一句稳定描述，格式为「关于 Ember……的」名词短语。描述要能代表这个方向下经历共同回答的问题，避免混入只属于个别经历的词汇。
3. 把每条经历归属到方向：默认归属一条；确实横跨多条的可归属多条；与 Ember 无实质关联的可以不归属。

经历记录：
""" + corpus + """

只输出 JSON，不要任何其他文字：
{"directions": [{"id": "dir_...", "description": "..."}], "assignments": {"经历ID": ["dir_..."]}}
"""
    return prompt


def strip_ansi(s):
    return ANSI_RE.sub("", s)


def extract_json(text):
    text = strip_ansi(text)
    # 取最后一个平衡的 {...} 块
    start = text.rfind("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
        start = text.rfind("{", 0, start)
    return None


def run_qwen(prompt):
    r = subprocess.run(
        ["docker", "exec", "-i", "ombre-ollama", "ollama", "run", "qwen3:1.7b"],
        input=prompt.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    return r.stdout.decode("utf-8", errors="replace")


def main():
    events = load_entity_events()
    print(f"实体 {ENTITY} 下 Event 数: {len(events)}", file=sys.stderr)
    prompt = build_prompt(events)
    print("--- PROMPT 已构建，长度:", len(prompt), file=sys.stderr)

    out = run_qwen(prompt)
    data = extract_json(out)
    if data is None:
        print("!!! JSON 提取失败，原始输出尾部：", file=sys.stderr)
        print(strip_ansi(out)[-2000:])
        sys.exit(1)

    print(json.dumps(data, ensure_ascii=False, indent=2))


    # 兼容三种形态：{"directions":..,"assignments":..} / 裸 assignments / 混合
    if not isinstance(data, dict):
        print("!!! 输出不是 JSON 对象"); sys.exit(1)
    assignments = data.get("assignments")
    if assignments is None and all(isinstance(v, list) for v in data.values()):
        assignments = data
        data = {"assignments": assignments}
    # 模型可能只回 assignments，directions 缺失时从 assignments 反推 id 集
    dirs = data.get("directions")
    if not dirs and "assignments" in data:
        ids = []
        for v in data["assignments"].values():
            for d in v:
                if d not in ids:
                    ids.append(d)
        dirs = [{"id": d, "description": "(模型未给出描述)"} for d in ids]
    print("== Directions ==")
    for d in dirs or []:
        print(f"  {d.get('id')}  {d.get('description','')}")
    print("== Assignments ==")
    for k, v in (data.get("assignments") or {}).items():
        print(f"  {k} -> {','.join(v)}")

    # 对照手动划分
    manual = {
        "mechanism": ["EVT-001-042738", "EVT-002-c0ef05", "EVT-003-49c6a9", "EVT-012-4c6e6b", "EVT-013-84de42"],
        "identity": ["EVT-007-1df1be", "EVT-008-15098d", "EVT-009-936f69", "EVT-012-4c6e6b", "EVT-013-84de42"],
        "memory_incidents": ["EVT-010-bc8bdd", "EVT-011-4d272c"],
        "frontend": ["EVT-004-e7e4ed", "EVT-005-29733b"],
    }
    print("== 对照：手动 4 方向 ==")
    for k, v in manual.items():
        print(f"  {k}: {', '.join(x.split('-')[0] for x in v)}")


if __name__ == "__main__":
    main()
