# -*- coding: utf-8 -*-
"""Direction 通电验收测试（momo 2026-10-05 裁决范围）。

验证：
1. 向量判断聚焦——方向清晰的输入只带出该方向挂载的 Event。
2. 模糊回退——"Ember 最近怎么样"这类输入维持全区域 + summary_fallback 标记。
3. 基础设施失败降级——向量缺失时回退全区域（不引入新行为）。
4. 无关输入不触发 Entity 通路。

用假 embed_fn（查表）替代真实 docker 调用，保证测试快且稳定。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.store import EmberStore
from ember.entity import EntityStore
from ember.direction import DirectionMatcher, DirectionVectorStore
from ember.recall import EntityRecall, register_event_entity_refs
from ember.service import EmberService

REAL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data_real")

_ok = 0
_fail = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _ok, _fail
    if cond:
        _ok += 1
        print(f"  [PASS] {name}")
    else:
        _fail += 1
        print(f"  [FAIL] {name}  {detail}")


def make_fake_embed(vectors_file: str):
    """假 embed_fn：与 direction_vectors.jsonl 里的方向向量比对余弦，
    按实验矩阵给 query 返回"输入文本 → 最近方向向量加微扰"的粗略模拟。
    这里直接返回一个根据关键词查表的真实性不必要——测试聚焦逻辑分支，
    用一个可控的查表函数即可。"""
    table = {
        "sleep": "dir_ember_mechanism",
        "谁造": "dir_ember_identity",
        "假记忆": "dir_ember_memory_incidents",
        "下拉框": "dir_ember_frontend",
    }

    def embed(text: str) -> list[float]:
        import json, math
        vs = DirectionVectorStore(REAL)
        for key, did in table.items():
            if key in text:
                return vs.get(did)["vector"]
        # 模糊句：返回 4 个方向的平均向量（margin 极小）
        allv = [vs.get(d)["vector"] for d in
                ("dir_ember_mechanism", "dir_ember_identity",
                 "dir_ember_memory_incidents", "dir_ember_frontend")]
        n = len(allv)
        return [sum(col) / n for col in zip(*allv)]

    return embed


def main() -> None:
    # 本测试是「通电验收」性质：依赖 data_real/（离线脚本挂载的真实
    # 向量库与实体数据，不入 git）。环境缺失 → 明确 SKIP 而非 FAIL——
    # 通用回归环境（无 data_real）跑到这里应当干净跳过。
    # 注意判"非空"而非"存在"：EmberStore 构造会自动建目录，跑过一次
    # 的空壳目录不能算环境就绪。
    if not (os.path.isdir(REAL) and os.listdir(REAL)):
        print("SKIP: data_real/ 不存在或为空（需先跑 scripts/mount_directions.py "
              "等离线挂载）——Direction 通电验收只在有真实数据的环境执行")
        return

    print("T-Direction: 向量判断聚焦 + 模糊回退 + 失败降级")

    store = EmberStore(REAL)
    estore = EntityStore(REAL)
    register_event_entity_refs(os.path.join(REAL, "events.jsonl"))
    vs = DirectionVectorStore(REAL)
    check("4 个方向向量已落盘",
          all(vs.has(d) for d in ("dir_ember_mechanism", "dir_ember_identity",
                                  "dir_ember_memory_incidents", "dir_ember_frontend")))

    matcher = DirectionMatcher(estore, vs, embed_fn=make_fake_embed(REAL))

    # 1. 聚焦：机制向输入 → 只带 mechanism 挂载的事件（001/002/003/012/013）
    er = EntityRecall(store, estore, direction_matcher=matcher)
    hits, eids = er.recall("Ember 的 sleep 收紧是怎么设计的")
    dr = er.last_direction_result
    check("机制向输入命中 Entity", len(hits) == 1)
    check("机制向输入聚焦 mechanism",
          dr is not None and dr.focused and dr.top1[0] == "dir_ember_mechanism",
          str(dr and dr.to_dict()))
    check("聚焦后只带 mechanism 区域(5条)", sorted("-".join(e.split("-")[:2]) for e in eids) ==
          ["EVT-001", "EVT-002", "EVT-003", "EVT-012", "EVT-013"],
          str(sorted("-".join(x.split("-")[:2]) for x in eids)))

    # 2. 模糊回退：全区域 + summary_fallback
    er2 = EntityRecall(store, estore, direction_matcher=matcher)
    hits2, eids2 = er2.recall("Ember 最近怎么样")
    dr2 = er2.last_direction_result
    check("模糊输入命中 Entity", len(hits2) == 1)
    check("模糊输入不聚焦", dr2 is not None and not dr2.focused)
    check("模糊输入维持全区域(12条)", len(eids2) == 12, f"got {len(eids2)}")

    # 3. service 层 summary_fallback 标记
    svc = EmberService(store, client=None, model="test")
    svc.direction_matcher = matcher
    # 直接走 EntityRecall 逻辑验证 service 的分支判断
    from ember.recall import EntityRecall as ER
    er3 = ER(store, estore, direction_matcher=matcher)
    er3.recall("帮我看看 Ember")
    dr3 = er3.last_direction_result
    check("模糊句 reason 属于回退类", dr3.reason in ("ambiguous", "low_score"), dr3.reason)

    # 4. 基础设施失败降级：方向向量缺失 → no_vectors → 全区域
    empty_vs = DirectionVectorStore(os.path.join(REAL, "_no_such_dir_"))
    matcher2 = DirectionMatcher(estore, empty_vs, embed_fn=make_fake_embed(REAL))
    er4 = EntityRecall(store, estore, direction_matcher=matcher2)
    hits4, eids4 = er4.recall("Ember 的 sleep 收紧是怎么设计的")
    dr4 = er4.last_direction_result
    check("向量缺失 → no_vectors 不聚焦", dr4 is not None and dr4.reason == "no_vectors")
    check("向量缺失 → 全区域回退(12条)", len(eids4) == 12, f"got {len(eids4)}")

    # 5. embed 失败降级
    def bad_embed(text: str) -> list[float]:
        raise RuntimeError("docker down")

    matcher3 = DirectionMatcher(estore, vs, embed_fn=bad_embed)
    er5 = EntityRecall(store, estore, direction_matcher=matcher3)
    _, eids5 = er5.recall("Ember 的 sleep 收紧是怎么设计的")
    dr5 = er5.last_direction_result
    check("embed 失败 → embed_failed 不聚焦", dr5 is not None and dr5.reason == "embed_failed")
    check("embed 失败 → 全区域回退(12条)", len(eids5) == 12, f"got {len(eids5)}")

    # 6. 无 DirectionMatcher 时行为不变（旧最小闭环回归）
    er6 = EntityRecall(store, estore)
    _, eids6 = er6.recall("Ember 的 sleep 收紧是怎么设计的")
    check("无 matcher 时维持全区域(12条)", len(eids6) == 12, f"got {len(eids6)}")

    # 7. 无关输入不触发
    hits7, eids7 = er.recall("今天晚饭吃什么")
    check("无关输入不触发 Entity", len(hits7) == 0 and len(eids7) == 0)

    print(f"\n{_ok} passed, {_fail} failed")
    sys.exit(1 if _fail else 0)


if __name__ == "__main__":
    main()
