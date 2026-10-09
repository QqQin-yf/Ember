# -*- coding: utf-8 -*-
"""Ember v3 MVP — 第一阶段验收测试。

验收标准（momo 2026-09-30 指定）：
对话 → 写入 Event → 生成至少一条 Recall Route → 下一次输入命中
→ 返回完整 Event → PresentationContext 记录 recall trace
→ LLM 能看到"为什么想起这件事"
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.models import Event, RecallRoute, FORBIDDEN_EVENT_FIELDS
from ember.recall import DirectRecall
from ember.store import EmberStore

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def make_event(event_id: str, summary: str, raw: str) -> Event:
    return Event(
        event_id=event_id,
        version_id="v1",
        source_ref="conversation:test-session",
        raw_ref=f"transcript://{raw}",
        event_time="2026-09-30T19:00:00+08:00",
        ingest_time="2026-09-30T19:01:00+08:00",
        turn_refs=["turn_001"],
        summary=summary,
        participants=["momo", "猪猪"],
        objects=["Ember", "记忆系统"],
        context=["Ember v3 定稿后的第一次落地"],
        core="我们讨论了 Ember v3 的 MVP 范围，确定了第一阶段的纵向闭环。",
        outcome=["确定了第一阶段的纵向闭环"],
        state=["架构文档已整合完毕"],
        affect=["兴奋", "专注"],
        significance="Ember 第一次真正落地的起点",
        details=["第一阶段只做 lexical 直达召回", "不做 embedding / LLM / Sleep"],
        provenance_refs=["session:2026-09-30-17-06-35"],
    )


def main():
    tmp = tempfile.mkdtemp(prefix="ember_mvp_test_")
    try:
        print("== 1. 写入 Event ==")
        store = EmberStore(tmp)
        e1 = make_event("evt_001", "momo 和猪猪敲定了 Ember v3 第一阶段的 MVP 范围",
                        "2026-09-30/turn_001")
        store.write_event(e1)
        check("Event 写入成功", store.get_event("evt_001").event_id == "evt_001")

        print("== 2. 生成 Recall Route ==")
        store.write_route(RecallRoute(
            route_id="rt_001", event_id="evt_001",
            matcher="lexical", cue_type="lexical",
            cue="Ember", evidence_refs=["turn_001"],
            provenance_refs=["memory-writer:baseline"],
        ))
        store.write_route(RecallRoute(
            route_id="rt_002", event_id="evt_001",
            matcher="lexical", cue_type="lexical",
            cue="纵向闭环", evidence_refs=["turn_001"],
            provenance_refs=["memory-writer:baseline"],
        ))
        check("两条 route 已写入", len(store.routes_for_event("evt_001")) == 2)
        check("索引包含 cue Ember", "ember" in store.index_snapshot())
        check("索引包含 cue 纵向闭环", "纵向闭环" in store.index_snapshot())

        print("== 3. 运行时字段禁止写回 Event ==")
        bad = e1.to_dict()
        bad["activation"] = 0.7
        try:
            Event.from_dict(bad)
            check("from_dict 拒绝 activation", False, "竟然没有抛异常")
        except ValueError as ex:
            check("from_dict 拒绝 activation", "activation" in str(ex), str(ex))

        print("== 4. Direct Recall：输入命中 ==")
        recall = DirectRecall(store)
        ctx = recall.recall("我们上次聊 Ember 聊到哪里了？")
        check("命中 evt_001", "evt_001" in ctx.recalled_events,
              str(ctx.recalled_events))
        check("trace 记录了 anchor", len(ctx.recall_trace.anchors) >= 1)
        anchor = ctx.recall_trace.anchors[0]
        check("anchor 记录 route_id", anchor.route_id == "rt_001")
        check("anchor 记录命中 cue", anchor.cue == "Ember")

        print("== 5. Presentation：LLM 能看到为什么想起 ==")
        pres = recall.build_presentation(ctx)
        check("presentation 包含完整 Event 内容",
              pres["recalled_events"][0]["event"]["summary"] == e1.summary)
        check("presentation 包含 raw_ref",
              pres["recalled_events"][0]["raw_ref"] == e1.raw_ref)
        why = pres["recalled_events"][0]["why_recalled"][0]
        check("why_recalled 有 route_id", why["route_id"] == "rt_001")
        check("why_recalled 有 matcher（无 mode）", why["matcher"] == "lexical"
              and "mode" not in why)
        check("trace 顶层有 match_type",
              pres["recall_trace"]["initial_anchors"][0]["match_type"] == "lexical")

        print("== 6. 第二条 cue 命中 ==")
        ctx2 = recall.recall("那个纵向闭环的事情后来怎么样了")
        check("第二条 cue 也能命中", "evt_001" in ctx2.recalled_events,
              str(ctx2.recalled_events))
        check("命中的 match_type 正确",
              any(h["match_type"] == "lexical"
                  for h in recall.build_presentation(ctx2)["recall_trace"]["initial_anchors"]))

        print("== 7. 不相关输入不命中 ==")
        ctx3 = recall.recall("今天天气怎么样")
        check("无命中", ctx3.recalled_events == [], str(ctx3.recalled_events))

        print("== 8. 索引可重建（重开 store）==")
        store2 = EmberStore(tmp)
        check("重开后 cue 索引一致",
              store2.index_snapshot() == store.index_snapshot())
        ctx4 = DirectRecall(store2).recall("Ember 后面做什么")
        check("重开后仍可命中", ctx4.recalled_events == ["evt_001"])

        print("== 9. retire route 后不再命中 ==")
        store2.retire_route("rt_001")
        ctx5 = DirectRecall(store2).recall("Ember 后面做什么")
        check("retired cue 不再命中", ctx5.recalled_events == [],
              str(ctx5.recalled_events))

        print()
        print(f"结果：{PASS} 通过，{FAIL} 失败")
        if FAIL:
            sys.exit(1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
