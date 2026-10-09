# -*- coding: utf-8 -*-
"""实验：Direction 匹配的相似度分布（为 baseline 阈值取数）。

用 docker exec 调 ombre-ollama 的 bge-m3，对 4 个方向描述与一组
测试 query 算余弦相似度矩阵。只做实验，不改任何数据文件。

铁律：实验产出的阈值是 baseline 假设，不是已证明设计。
"""
import json
import math
import subprocess
import sys

BASE = r"D:\ruanjian\workbuddy项目文件夹\2026-09-30-17-06-35\ember-v3-mvp"

DIRECTIONS = [
    ("dir_ember_mechanism", "关于 Ember 的架构与机制如何设计、为什么这样设计，包括传播模型、Sleep 收紧、写入策略等设计裁决。"),
    ("dir_ember_identity", "关于 Ember 如何认识自己：它的版本来历、建造者是谁、它经历过哪些认知反转。"),
    ("dir_ember_memory_incidents", "关于 Ember 身上发生过的记忆事故与验证：假记忆、记忆连续性、上下文与记忆的关系。"),
    ("dir_ember_frontend", "关于 Ember 测试前端的功能与使用：界面做了什么改动、功能怎么验证。"),
]

# 覆盖各类输入：明确指向 / 模糊 / 与 Ember 无关
QUERIES = [
    # 机制向
    "Ember 的 sleep 阶段 route 收紧是怎么设计的",
    "你为什么不搞激活扩散了",
    "写入策略是每句话都写吗",
    # 身份向
    "Ember 你知道自己是谁造的吗",
    "你的第一版架构是什么样的",
    # 事故向
    "那个假记忆的事到底怎么回事",
    "上下文清空之后你怎么还记得",
    # 前端向
    "前端下拉框加好了吗",
    "测试界面还能怎么用",
    # 模糊：指到 entity 但方向不明
    "Ember 最近怎么样",
    "帮我看看 Ember",
    # 完全无关
    "今天天气不错",
    "晚饭吃什么好",
]


def embed(text: str) -> list[float]:
    """docker exec 调 bge-m3：prompt 从 stdin 进，stdout 出 embedding。"""
    r = subprocess.run(
        ["docker", "exec", "-i", "ombre-ollama", "ollama", "run", "bge-m3"],
        input=text.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=120,
    )
    out = r.stdout.decode("utf-8", errors="replace").strip()
    return json.loads(out)


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def main() -> None:
    dir_vecs = []
    for did, desc in DIRECTIONS:
        v = embed(desc)
        dir_vecs.append((did, v))
        print(f"embedded {did} dim={len(v)}", file=sys.stderr)

    print("\n=== 相似度矩阵（行=query，列=方向） ===")
    header = "query".ljust(28) + "".join(
        d[0].replace("dir_ember_", "").ljust(22) for d in DIRECTIONS)
    print(header)

    q_vecs = []
    for q in QUERIES:
        v = embed(q)
        q_vecs.append(v)
        sims = [cosine(v, dv) for _, dv in dir_vecs]
        row = q[:26].ljust(28) + "".join(f"{s:.4f}".ljust(22) for s in sims)
        print(row)

    print("\n=== 每条 query 的 top1 / margin（top1 - top2） ===")
    for q, v in zip(QUERIES, q_vecs):
        sims = sorted(((cosine(v, dv), did) for did, dv in dir_vecs), reverse=True)
        top1, top2 = sims[0], sims[1]
        print(f"{q[:26].ljust(28)} top1={top1[1]}({top1[0]:.4f})  "
              f"top2={top2[1]}({top2[0]:.4f})  margin={top1[0]-top2[0]:.4f}")


if __name__ == "__main__":
    main()
