# -*- coding: utf-8 -*-
"""实验：Direction 匹配相似度分布 v2（Qwen3-Embedding-8B 阈值复标）。

被谁调用：手工离线运行（scripts/exp_direction_match_v2.py）。
背景：§5.3 聚焦判据 top1>=0.47 / margin>=0.08 是 bge-m3 实测；
     2026-10-09 换型 Qwen/Qwen3-Embedding-8B 后向量空间迁移，
     旧数值不可复用，本实验为新模型重新取数。

做法（与 v1 exp_direction_match.py 同流程）：
  方向向量直接读 data_real/direction_vectors.jsonl（运行时用的就是
  这份，保证实验与线上同数据）；query 逐条调 ember.direction.embed_text
  （硅基流动 API）算与 4 个方向的余弦；按人工标注的期望方向分
  正例/陷阱/模糊/无关四组统计 top1 与 margin(top1-top2) 分布。

产出：只打印报告，不改任何数据文件与代码。
铁律：实验产出的阈值是 baseline 假设，不是已证明设计。
"""
from __future__ import annotations

import json
import math
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember.direction import embed_text  # noqa: E402
from ember.env import load_env  # noqa: E402

load_env(BASE)

VEC_FILE = os.path.join(BASE, "data_real", "direction_vectors.jsonl")

# 期望方向标注：None=模糊（entity 明确但方向不明，期望 margin 不足被拒）
# "none"=与 Ember 无关（期望 top1 低）
QUERIES: list[tuple[str, str | None]] = [
    # --- 机制向 dir_ember_mechanism ---
    ("Ember 的 sleep 阶段 route 收紧是怎么设计的", "dir_ember_mechanism"),
    ("你为什么不搞激活扩散了", "dir_ember_mechanism"),
    ("写入策略是每句话都写吗", "dir_ember_mechanism"),
    ("Sleep 的候选组装是靠召回记账吗", "dir_ember_mechanism"),
    ("说说 Entity 浮现那一套的裁决", "dir_ember_mechanism"),
    ("Direction 为什么要限制数量", "dir_ember_mechanism"),
    # --- 身份向 dir_ember_identity ---
    ("Ember 你知道自己是谁造的吗", "dir_ember_identity"),
    ("你的第一版架构是什么样的", "dir_ember_identity"),
    ("你经历过的那次认知反转是怎么回事", "dir_ember_identity"),
    ("你还记得你的版本从哪来的吗", "dir_ember_identity"),
    # --- 事故向 dir_ember_memory_incidents ---
    ("那个假记忆的事到底怎么回事", "dir_ember_memory_incidents"),
    ("上下文清空之后你怎么还记得", "dir_ember_memory_incidents"),
    ("你验证过自己的记忆没出过错吗", "dir_ember_memory_incidents"),
    ("记忆连续性那档子事后来怎么解决的", "dir_ember_memory_incidents"),
    # --- 前端向 dir_ember_frontend ---
    ("前端下拉框加好了吗", "dir_ember_frontend"),
    ("测试界面还能怎么用", "dir_ember_frontend"),
    ("Ember 前端最近做了什么改动", "dir_ember_frontend"),
    ("界面上那些功能怎么验证", "dir_ember_frontend"),
    # --- 陷阱题：近义但跨向（期望 top1 落错或 margin 不足） ---
    ("你怎么把这些架构裁决都记住的", "dir_ember_memory_incidents"),
    ("前端报错会不会是你的记忆出了问题", "dir_ember_memory_incidents"),
    # --- 模糊：entity 明确、方向不明（期望 margin 不足 → 模糊回退） ---
    ("Ember 最近怎么样", None),
    ("帮我看看 Ember", None),
    ("聊聊 Ember 吧", None),
    ("Ember 有什么新进展", None),
    # --- 无关（期望 top1 低） ---
    ("今天天气不错", "none"),
    ("晚饭吃什么好", "none"),
    ("堂姐的婚礼准备的怎么样了", "none"),
]


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def load_dir_vecs() -> list[tuple[str, str, list[float]]]:
    """运行时同源的 Direction 向量（direction_id, description 需另查）。"""
    rows = []
    for line in open(VEC_FILE, encoding="utf-8"):
        line = line.strip()
        if line:
            d = json.loads(line)
            rows.append((d["direction_id"], d["vector"]))
    return rows


def main() -> None:
    dir_vecs = load_dir_vecs()
    print(f"方向向量 {len(dir_vecs)} 个（读自 direction_vectors.jsonl）\n",
          file=sys.stderr)

    print("=== 相似度矩阵（行=query，列=方向）===")
    short = [d[0].replace("dir_ember_", "") for d in dir_vecs]
    header = "query".ljust(26) + "".join(s.ljust(20) for s in short)
    print(header)

    results: list[dict] = []
    for q, expected in QUERIES:
        v = embed_text(q)
        sims = sorted(((cosine(v, dv), did)
                       for did, dv in dir_vecs), reverse=True)
        top1, top2 = sims[0], sims[1]
        margin = top1[0] - top2[0]
        results.append({"q": q, "expected": expected,
                        "top1": top1, "top2": top2, "margin": margin})
        mark = {"mech": ""}.get("mech", "")  # 占位，标记在行尾
        print(f"{q[:24].ljust(26)}"
              + "".join(f"{s:.4f}".ljust(20) for s, _ in sims)
              + f"  top1={top1[1].replace('dir_ember_', '')} "
                f"margin={margin:.4f}")

    def stats(rows: list[dict], label: str, key: str) -> None:
        if not rows:
            return
        tops = sorted(r["top1"][0] for r in rows)
        margins = sorted(r["margin"] for r in rows)
        n = len(rows)
        print(f"{label}  n={n}")
        print(f"  top1   min={tops[0]:.4f}  p25={tops[n//4]:.4f}  "
              f"med={tops[n//2]:.4f}  max={tops[-1]:.4f}")
        print(f"  margin min={margins[0]:.4f}  p25={margins[n//4]:.4f}  "
              f"med={margins[n//2]:.4f}  max={margins[-1]:.4f}")

    print("\n=== 分组分布 ===")
    # 正例 = top1 恰好是期望方向的
    correct = [r for r in results if r["expected"] not in (None, "none")
               and r["top1"][1] == r["expected"]]
    wrong = [r for r in results if r["expected"] not in (None, "none")
             and r["top1"][1] != r["expected"]]
    fuzzy = [r for r in results if r["expected"] is None]
    unrelated = [r for r in results if r["expected"] == "none"]
    stats(correct, f"正例（top1=期望方向）", "correct")
    stats(fuzzy, "模糊（期望被拒聚焦）", "fuzzy")
    stats(unrelated, "无关（期望 top1 低）", "unrelated")

    print("\n=== 正例明细（决定阈值下限）===")
    for r in sorted(correct, key=lambda x: x["top1"][0]):
        print(f"  top1={r['top1'][0]:.4f} margin={r['margin']:.4f}  {r['q']}")
    print("\n=== top1 落错的明细（决定 margin 需要多严）===")
    for r in wrong:
        print(f"  top1={r['top1'][1]}({r['top1'][0]:.4f}) "
              f"期望={r['expected']}  {r['q']}")
    print("\n=== 模糊组明细（决定阈值上限）===")
    for r in sorted(fuzzy, key=lambda x: -x["top1"][0]):
        print(f"  top1={r['top1'][1]}({r['top1'][0]:.4f}) "
              f"margin={r['margin']:.4f}  {r['q']}")
    print("\n=== 无关组 top1 ===")
    for r in unrelated:
        print(f"  top1={r['top1'][1]}({r['top1'][0]:.4f})  {r['q']}")


if __name__ == "__main__":
    main()
