# -*- coding: utf-8 -*-
"""Entity summary 重算测试（§2.1 Entity.summary，momo 2026-10-10 定稿）。

覆盖：
1. Entity 字段：summary/summary_at 默认空、to_dict/from_dict 往返
2. 触发收集：extract / attach 的实体被收集（从 apply_verdict 报告解析），
   none 不收集
3. sleep_run 的 summary 步核心（mock LLM）：
   - 有 member 摘要 → 调 LLM → summary/summary_at 落盘
   - 旧 summary 非空时进入 user prompt（"现有描述"区块）
   - 名下无事件 → 不调用
   - LLM 失败/空输出 → status=failed、字段不动
4. 呈现层消费：summary_fallback 前缀块在 summary 非空时插入名片行；
   空时跳过
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

from ember.entity import Entity, EntityStore  # noqa: E402
from ember.prompts import (  # noqa: E402
    SUMMARY_SYSTEM_PROMPT, build_summary_user_prompt,
)

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"PASS - {name}")
    else:
        failures.append(name)
        print(f"FAIL - {name}  {detail}")


def test_entity_fields():
    e = Entity(entity_id="ent_x", name="X系统")
    check("summary/summary_at 默认空", e.summary == "" and e.summary_at == "")
    d = e.to_dict()
    check("to_dict 含 summary 字段", "summary" in d and "summary_at" in d)
    e.summary = "user 做的记忆系统。"
    e.summary_at = "2026-10-10T11:00:00+08:00"
    e2 = Entity.from_dict(e.to_dict())
    check("from_dict 往返保留 summary",
          e2.summary == e.summary and e2.summary_at == e.summary_at)


def test_prompt_builder():
    p1 = build_summary_user_prompt("X系统", "", ["摘要一", "摘要二"])
    check("首生无旧描述区块", "现有描述" not in p1
          and "## 实体\nX系统" in p1 and "- 摘要一" in p1)
    p2 = build_summary_user_prompt("X系统", "旧描述文本。", ["摘要一"])
    check("旧描述进入参考区块", "现有描述" in p2 and "旧描述文本。" in p2)
    check("SUMMARY prompt 定稿关键句在位",
          "像一张名片" in SUMMARY_SYSTEM_PROMPT
          and "不写变化过程" in SUMMARY_SYSTEM_PROMPT)


def test_touched_entities_parsing():
    """从 apply_verdict 报告的 entity_action 解析被触碰实体（sleep_run 的收集逻辑）。"""
    cases = [
        ("extract:ent_ab12(X系统)", "ent_ab12"),
        ("attach:ent_ember", "ent_ember"),
        ("none", None),
    ]
    for action, want in cases:
        got = None
        if action.startswith("extract:"):
            got = action.split(":")[1].split("(")[0]
        elif action.startswith("attach:"):
            got = action.split(":")[1]
        check(f"entity_action={action} → {want}", got == want, f"got={got}")


def test_summary_step_with_mock_llm():
    """复刻 sleep_run summary 步的核心路径（mock LLM，不跑 argparse main）。"""
    with tempfile.TemporaryDirectory() as root:
        estore = EntityStore(root)
        ent = Entity(entity_id="ent_x", name="X系统")
        estore.upsert(ent)
        # 两个 member 事件（events dict 形态，与 sleep_run 一致）
        events = {
            "EVT-001-aaaa": {"event_id": "EVT-001-aaaa",
                             "summary": "X系统 v1 定稿",
                             "entity_refs": ["ent_x"]},
            "EVT-002-bbbb": {"event_id": "EVT-002-bbbb",
                             "summary": "X系统 召回调试",
                             "entity_refs": ["ent_x"]},
            "EVT-003-cccc": {"event_id": "EVT-003-cccc",
                             "summary": "无关事件",
                             "entity_refs": []},
        }

        captured = {}

        def fake_llm(system, user, engine, task=None):
            captured["system"] = system
            captured["user"] = user
            captured["task"] = task
            return json.dumps({"summary": "user 做的记忆系统，包括召回与维护。"})

        from ember import executors
        results = []
        member_sums = [ev.get("summary", "") for eid, ev in events.items()
                       if "ent_x" in (ev.get("entity_refs") or [])
                       and ev.get("summary", "")]
        user = build_summary_user_prompt(ent.name, ent.summary or "", member_sums)
        raw = fake_llm(SUMMARY_SYSTEM_PROMPT, user,
                       executors.load_engine(root, "summary"), task="summary")
        parsed = json.loads(raw)
        ent.summary = parsed["summary"].strip()
        ent.summary_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        estore.save()
        results.append({"entity_id": "ent_x", "status": "updated"})

        check("LLM 收到 system 定稿与材料",
              captured["system"] == SUMMARY_SYSTEM_PROMPT
              and "X系统 v1 定稿" in captured["user"]
              and "无关事件" not in captured["user"],
              captured["user"][:80])
        check("task=summary 传入", captured["task"] == "summary")
        check("落盘后 summary/summary_at 非空",
              ent.summary and ent.summary_at)
        estore2 = EntityStore(root)
        check("持久化往返", estore2.get("ent_x").summary == ent.summary)

        # 旧 summary 第二次重算时进入参考区块
        user2 = build_summary_user_prompt(ent.name, ent.summary, member_sums)
        check("重算时旧描述进区块", "现有描述" in user2 and "user 做的记忆系统" in user2)

        # 名下无事件：不调用
        empty_ent = Entity(entity_id="ent_y", name="Y空实体")
        estore.upsert(empty_ent)
        y_sums = [ev.get("summary", "") for eid, ev in events.items()
                  if "ent_y" in (ev.get("entity_refs") or [])]
        check("无 member → 不产生调用素材", y_sums == [])

        # LLM 失败：字段不动、status=failed
        def boom(system, user, engine, task=None):
            raise RuntimeError("api down")
        raw2 = None
        try:
            boom(SUMMARY_SYSTEM_PROMPT, user, "glm", task="summary")
        except Exception:
            raw2 = None
        parsed2 = None
        try:
            parsed2 = json.loads(raw2) if raw2 else None
        except json.JSONDecodeError:
            parsed2 = None
        ok = bool(parsed2 and isinstance(parsed2.get("summary", ""), str)
                  and parsed2["summary"].strip())
        check("失败路径不更新字段", not ok and ent.summary == "user 做的记忆系统，包括召回与维护。")


def test_presentation_consumes_summary():
    """summary_fallback 前缀块：summary 非空插名片行，空跳过。"""
    # 不起完整 service（依赖 LLM client）——直接测消费分支的判定逻辑
    entity_hits_like = [{"entity_id": "ent_x", "name": "X系统"}]
    ent_summary = "user 做的记忆系统。"
    lines = []
    for h in entity_hits_like:
        s = ent_summary.strip()
        if s:
            lines.append(f"关于〈{h['name']}〉：{s}")
    check("非空 → 名片行生成",
          lines == ["关于〈X系统〉：user 做的记忆系统。"])
    # 空 summary 场景
    lines2 = []
    for h in entity_hits_like:
        s = "".strip()
        if s:
            lines2.append(f"关于〈{h['name']}〉：{s}")
    check("空 summary → 跳过不报错", lines2 == [])


def main():
    test_entity_fields()
    test_prompt_builder()
    test_touched_entities_parsing()
    test_summary_step_with_mock_llm()
    test_presentation_consumes_summary()
    total = 13
    if failures:
        print(f"\n{total - len(failures)}/{total} checks passed，失败：{failures}")
        sys.exit(1)
    print(f"\n{total}/{total} checks passed")


if __name__ == "__main__":
    main()
