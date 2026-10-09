# -*- coding: utf-8 -*-
"""Cue semantic 管线测试（§2.4 匹配方式统一为 semantic，A 方案）。

覆盖：
1. CueVectorStore：加载/缺失文件空表/has/get/size
2. semantic_hits：命中（≥floor）/不命中（<floor）/无向量 no_vectors /
   embed 失败降级 embed_failed
3. DirectRecall 全链：
   - 词面命中共存：同一 cue 词面命中后不再进语义段（matcher 仍 lexical）
   - 语义扩展命中：词面零命中的 cue 通过 mock 向量命中（matcher=semantic）
   - multi_cue 聚合：词面+语义命中共通计入（同 cue 文本只算一个触点）
   - 降级：embed 抛异常 → 纯词面结果不变
   - 无向量文件 → no_vectors，行为=纯词面（原行为不引入新行为）
4. 记账兼容：semantic 命中进 anchors 后，RecallLedger 记账路径照常
   （route=cue 行、cue 列为命中 cue 文本——matcher 不影响记账形态）
"""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember.cue_semantic import CueVectorStore, semantic_hits, CUE_SEMANTIC_FLOOR  # noqa: E402
from ember.models import Event, RecallRoute  # noqa: E402
from ember.recall import DirectRecall  # noqa: E402
from ember.recall_ledger import RecallLedger  # noqa: E402
from ember.store import EmberStore  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"PASS - {name}")
    else:
        failures.append(name)
        print(f"FAIL - {name}  {detail}")


def _mk_store(root: str) -> EmberStore:
    store = EmberStore(root)
    e1 = Event(event_id="EVT-001-aaaa", version_id="v1",
               source_ref="sess1#1", raw_ref="sess1#1",
               event_time="2026-10-09T10:00:00+08:00",
               ingest_time="2026-10-09T10:00:00+08:00",
               summary="国庆去河南参加堂姐婚礼", core="国庆假期去了河南，参加堂姐的婚礼。")
    store.write_event(e1)
    e2 = Event(event_id="EVT-002-bbbb", version_id="v2",
               source_ref="sess1#2", raw_ref="sess1#2",
               event_time="2026-10-09T11:00:00+08:00",
               ingest_time="2026-10-09T11:00:00+08:00",
               summary="Ember 记忆系统 sleep 设计定稿", core="讨论并定稿了 Ember 的 sleep 设计。")
    store.write_event(e2)
    # route 1：词面 cue（会被"堂姐结婚了吧"词面命中一部分）
    store.write_route(RecallRoute(
        route_id="rt_001_01", event_id=e1.event_id, matcher="lexical",
        cue_type="lexical", cue="堂姐结婚"))
    # route 2：词面 cue，但用户输入"她办喜事那次"词面零命中
    store.write_route(RecallRoute(
        route_id="rt_001_02", event_id=e1.event_id, matcher="lexical",
        cue_type="conceptual", cue="婚礼"))
    # route 3：sleep 设计的 cue
    store.write_route(RecallRoute(
        route_id="rt_002_01", event_id=e2.event_id, matcher="lexical",
        cue_type="lexical", cue="sleep 设计"))
    return store


def _mk_vectors(root: str, vecs: dict[str, list[float]]):
    with open(os.path.join(root, "cue_vectors.jsonl"), "w",
              encoding="utf-8") as f:
        for rid, vec in vecs.items():
            f.write(json.dumps({"route_id": rid, "cue": rid,
                                "vector": vec, "model": "mock"}) + "\n")


def _vec_of(cue: str) -> list[float]:
    """确定性 3 维 mock 向量：词义空间里 办事/婚礼/结婚 相近，sleep 远。"""
    table = {
        "堂姐结婚": [0.9, 0.8, 0.0],
        "婚礼": [0.9, 0.9, 0.0],
        "sleep 设计": [0.0, 0.0, 1.0],
        "办喜事": [0.9, 0.9, 0.0],   # 与婚礼近乎同向
        "无关": [0.0, 0.5, 0.0],
    }
    return table[cue]


def test_vector_store():
    with tempfile.TemporaryDirectory() as root:
        empty = CueVectorStore(root)
        check("CueVectorStore 缺文件=空表", empty.size() == 0
              and not empty.has("rt_x"))
        _mk_vectors(root, {"rt_001_01": [1.0, 0.0, 0.0]})
        loaded = CueVectorStore(root)
        check("CueVectorStore 加载/has/get",
              loaded.has("rt_001_01")
              and loaded.get("rt_001_01")["vector"] == [1.0, 0.0, 0.0])


def test_semantic_hits_unit():
    with tempfile.TemporaryDirectory() as root:
        _mk_vectors(root, {
            "rt_hit": _vec_of("婚礼"), "rt_miss": _vec_of("sleep 设计")})

        class R:
            def __init__(self, rid):
                self.route_id = rid

        vs = CueVectorStore(root)
        # 输入向量与婚礼同向 → rt_hit 命中、rt_miss 不中
        hits, reason = semantic_hits(
            "她办喜事那次", [R("rt_hit"), R("rt_miss")], vs,
            floor=0.99, embed_fn=lambda t: _vec_of("办喜事"))
        check("semantic_hits 命中/不命中分判",
              reason == "ok" and [r.route_id for r in hits] == ["rt_hit"],
              f"reason={reason} hits={[r.route_id for r in hits]}")
        # 无任何向量库 → no_vectors
        empty = CueVectorStore(tempfile.mkdtemp())
        hits2, reason2 = semantic_hits(
            "x", [R("rt_hit")], empty, embed_fn=lambda t: [1.0])
        check("semantic_hits 无向量=no_vectors",
              reason2 == "no_vectors" and hits2 == [])
        # embed 抛异常 → embed_failed
        def _boom(t):
            raise RuntimeError("api down")
        hits3, reason3 = semantic_hits(
            "x", [R("rt_hit")], vs, embed_fn=_boom)
        check("semantic_hits embed 失败降级",
              reason3 == "embed_failed" and hits3 == [])


def test_direct_recall_integration():
    with tempfile.TemporaryDirectory() as root:
        store = _mk_store(root)
        _mk_vectors(root, {
            "rt_001_01": _vec_of("堂姐结婚"),
            "rt_001_02": _vec_of("婚礼"),
            "rt_002_01": _vec_of("sleep 设计"),
        })

        # 场景 1：词面零命中 + 语义命中（"她办喜事那次" ≈ 婚礼/堂姐结婚）
        dr = DirectRecall(store, embed_fn=lambda t: _vec_of("办喜事"))
        ctx = dr.recall("她办喜事那次")
        matchers = {h.matcher for h in ctx.recall_trace.anchors}
        hit_cues = {h.cue for h in ctx.recall_trace.anchors}
        check("词面零命中→语义扩展命中",
              "semantic" in matchers and hit_cues == {"堂姐结婚", "婚礼"},
              f"anchors={[(h.cue, h.matcher) for h in ctx.recall_trace.anchors]}")
        check("语义命中带出事件", ctx.recalled_events == ["EVT-001-aaaa"],
              str(ctx.recalled_events))
        check("语义段 reason=ok", dr.last_semantic_reason == "ok",
              dr.last_semantic_reason)

        # 场景 2：词面命中共存——词面命中的 route 不重复进语义段
        dr2 = DirectRecall(store, embed_fn=lambda t: _vec_of("堂姐结婚"))
        ctx2 = dr2.recall("堂姐结婚")
        by_route = {h.route_id: h.matcher for h in ctx2.recall_trace.anchors}
        check("词面命中保持 lexical 不重复语义",
              by_route.get("rt_001_01") == "lexical",
              str(by_route))
        # "堂姐结婚"输入里无"婚礼"字样 → rt_001_02 词面未中，语义命中
        check("同事件第二 cue 语义补位",
              by_route.get("rt_001_02") == "semantic", str(by_route))
        check("multi_cue 聚合跨 matcher 生效",
              len({h.cue for h in ctx2.recall_trace.anchors}) == 2)

        # 场景 3：语义命中与词面命中共存排序（multi_cue 优先）
        ctx3 = dr2.recall("堂姐结婚 办喜事")
        first = ctx3.recalled_events[0] if ctx3.recalled_events else None
        check("共命中事件排前", first == "EVT-001-aaaa",
              str(ctx3.recalled_events))


def test_degradation():
    with tempfile.TemporaryDirectory() as root:
        store = _mk_store(root)
        _mk_vectors(root, {"rt_001_01": _vec_of("堂姐结婚"),
                           "rt_001_02": _vec_of("婚礼"),
                           "rt_002_01": _vec_of("sleep 设计")})

        def _boom(t):
            raise RuntimeError("api down")

        # embed 失败：纯词面照常，语义段整体降级
        dr = DirectRecall(store, embed_fn=_boom)
        ctx = dr.recall("堂姐结婚")
        matchers = {h.matcher for h in ctx.recall_trace.anchors}
        check("embed 失败→纯词面结果不变",
              matchers == {"lexical"}
              and ctx.recalled_events == ["EVT-001-aaaa"],
              f"matchers={matchers} events={ctx.recalled_events}")
        check("embed 失败 reason=embed_failed",
              dr.last_semantic_reason == "embed_failed",
              dr.last_semantic_reason)

        # 无向量文件：no_vectors，行为=纯词面
        # （须用没写过 cue_vectors.jsonl 的全新 root——CueVectorStore 启动时加载）
        with tempfile.TemporaryDirectory() as clean_root:
            store2 = _mk_store(clean_root)
            dr2 = DirectRecall(store2)
            ctx2 = dr2.recall("堂姐结婚")
            check("无向量文件=no_vectors 行为不变",
                  dr2.last_semantic_reason == "no_vectors"
                  and ctx2.recalled_events == ["EVT-001-aaaa"],
                  f"reason={dr2.last_semantic_reason}")


def test_ledger_compatibility():
    with tempfile.TemporaryDirectory() as root:
        store = _mk_store(root)
        _mk_vectors(root, {"rt_001_01": _vec_of("堂姐结婚"),
                           "rt_001_02": _vec_of("婚礼"),
                           "rt_002_01": _vec_of("sleep 设计")})
        dr = DirectRecall(store, embed_fn=lambda t: _vec_of("办喜事"))
        ctx = dr.recall("她办喜事那次")
        # 复刻 service._ledger_record 的 cue 段聚合口径
        ledger = RecallLedger(root)
        hits = [h for h in ctx.recall_trace.anchors
                if h.event_id in ctx.recalled_events]
        by_event: dict[str, list[str]] = {}
        for h in hits:
            by_event.setdefault(h.event_id, [])
            if h.cue not in by_event[h.event_id]:
                by_event[h.event_id].append(h.cue)
        merged_cues = set()
        for eid, cues in by_event.items():
            # 多 cue 共命中合并行（service._ledger_record 的既有口径：
            # cue 列 = 命中顺序拼接，非排序）
            if len(cues) > 1:
                merged = ";".join(cues)
                merged_cues.add(merged)
                ledger.record("sess1", 1, route="cue", event_ids=[eid],
                              cue=merged)
            ledger.record("sess1", 1, route="cue", event_ids=[eid],
                          cue=cues[0] if len(cues) == 1 else None)
        row = ledger.cue_use_count_for_event("EVT-001-aaaa", "婚礼")
        # 两条 cue 共命中时按既有口径只落合并行（cue 列 = "堂姐结婚;婚礼"），
        # 单 cue 行的 cue 列为 None——"婚礼"的计数经合并行生效
        merged_key = ";".join(by_event["EVT-001-aaaa"])
        row_merged = ledger.cue_use_count_for_event("EVT-001-aaaa", merged_key)
        check("semantic 命中照常进记账",
              row_merged >= 1,
              f"婚礼单独行={row} 合并行[{merged_key}]={row_merged}")
        check("多 cue 合并记账行存在",
              any(ledger.cue_use_count_for_event("EVT-001-aaaa", m) >= 1
                  for m in merged_cues),
              f"merged={merged_cues}")


def main():
    test_vector_store()
    test_semantic_hits_unit()
    test_direct_recall_integration()
    test_degradation()
    test_ledger_compatibility()
    total = 13
    if failures:
        print(f"\n{total - len(failures)}/{total} checks passed，失败：{failures}")
        sys.exit(1)
    print(f"\n{total}/{total} checks passed")


if __name__ == "__main__":
    main()
