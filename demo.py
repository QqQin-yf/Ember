# -*- coding: utf-8 -*-
"""Ember v3 MVP — 第一阶段纵向闭环演示。

完整流程：
对话 → 写入 Event → 生成至少一条 Recall Route
→ 下一次输入命中 → 返回完整 Event
→ PresentationContext 记录 recall trace
→ LLM 能看到"为什么想起这件事"
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ember.models import Event, RecallRoute
from ember.recall import DirectRecall
from ember.store import EmberStore

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def write_demo_experience(store: EmberStore) -> None:
    """白天：一段经历发生 → LLM Memory Writer 产出 Event + 初始 Recall Routes。"""
    event = Event(
        event_id="evt_20260930_001",
        version_id="v1",
        source_ref="conversation:2026-09-30-17-06-35",
        raw_ref="transcript://2026-09-30/turn_042",
        event_time="2026-09-30T17:32:00+08:00",
        ingest_time="2026-09-30T17:35:00+08:00",
        turn_refs=["turn_042"],
        summary="momo 定稿了 Ember v3 事件中心记忆架构，决定开始写 MVP。",
        participants=["momo", "猪猪"],
        objects=["Ember v3", "架构文档", "MVP"],
        context=["Ember 记忆系统研究", "v0.1 之后推翻重来"],
        core="我们给出了 5 个核心对象的完整 schema，做出了四项架构裁决，"
             "Ember 由此从一份设计稿变成了有第一块实体的工程。",
        outcome=["架构文档整合完毕", "确定第一阶段做纵向闭环"],
        state=["进入工程落地阶段"],
        affect=["兴奋", "笃定"],
        significance="Ember 从纯架构讨论第一次转入可运行代码的起点",
        details=[
            "核心 5 对象：Event / Recall Route / Event Relation / Episode Cluster / Presentation",
            "四项裁决：embedding 粗召回索引 Route、mode×matcher 正交、immutable current_cues、confidence 不进 gate",
        ],
        provenance_refs=["session:2026-09-30-17-06-35"],
    )
    store.write_event(event)

    # LLM Memory Writer 写下的初始 Recall Routes（白天轻写）
    store.write_route(RecallRoute(
        route_id="rt_20260930_001", event_id=event.event_id,
        matcher="lexical", cue_type="lexical",
        cue="Ember v3", evidence_refs=["turn_042"],
        provenance_refs=["memory-writer:baseline"],
    ))
    store.write_route(RecallRoute(
        route_id="rt_20260930_002", event_id=event.event_id,
        matcher="lexical", cue_type="lexical",
        cue="纵向闭环", evidence_refs=["turn_042"],
        provenance_refs=["memory-writer:baseline"],
    ))
    store.write_route(RecallRoute(
        route_id="rt_20260930_003", event_id=event.event_id,
        matcher="lexical", cue_type="lexical",
        cue="MVP", evidence_refs=["turn_042"],
        provenance_refs=["memory-writer:baseline"],
    ))


def main():
    fresh = "--fresh" in sys.argv
    if fresh and os.path.isdir(DATA_DIR):
        import shutil
        shutil.rmtree(DATA_DIR)

    store = EmberStore(DATA_DIR)
    if not store.all_events():
        print("[白天] 写入一段经历 + 初始 Recall Routes\n")
        write_demo_experience(store)
    else:
        print("[白天] 已有经历，跳过写入（数据目录持久化）\n")

    print("[夜晚？] 不，第一阶段没有 Sleep —— 结构保持写入时的样子\n")

    print("[未来] 输入到来：'Ember v3 那个纵向闭环我们做到哪了？'\n")
    recall = DirectRecall(store)
    ctx = recall.recall("Ember v3 那个纵向闭环我们做到哪了？")

    print("=" * 60)
    print("给 LLM 的 Presentation（完整 JSON）：")
    print("=" * 60)
    print(json.dumps(recall.build_presentation(ctx), ensure_ascii=False, indent=2))

    print()
    print("=" * 60)
    print("一句话：这件事发生过，现在提到它，它被直接想起来了。")
    print(f"为什么想起：输入命中 route {ctx.recall_trace.anchors[0].route_id}"
          f"（cue={ctx.recall_trace.anchors[0].cue!r}, "
          f"match={ctx.recall_trace.anchors[0].mode}+{ctx.recall_trace.anchors[0].matcher}）")
    print("整个召回过程没有用 LLM、没有 embedding、没有扩散。")


if __name__ == "__main__":
    main()
