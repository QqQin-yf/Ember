# -*- coding: utf-8 -*-
"""实测 Direction 匹配的延迟分解（冷/热、首次/二次）。只测时间，不改数据。"""
import time
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from ember.direction import docker_embed

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, BASE)


def timed(label, fn):
    t0 = time.perf_counter()
    r = fn()
    dt = (time.perf_counter() - t0) * 1000
    print(f"{label}: {dt:.0f} ms (dim={len(r) if isinstance(r, list) else '-'})")
    return dt


print("=== docker exec 通路（当前实现） ===")
timed("第 1 次调用（冷启动，模型可能要加载）", lambda: docker_embed("延迟测试"))
timed("第 2 次调用（热）", lambda: docker_embed("延迟测试第二条"))
timed("第 3 次调用（热）", lambda: docker_embed("Ember 的 sleep 收紧是怎么设计的"))

print("\n=== docker exec 固定开销（不跑模型，只 echo） ===")
import subprocess
timed("裸 docker exec echo", lambda: subprocess.run(
    ["docker", "exec", "ombre-ollama", "echo", "hi"],
    stdout=subprocess.PIPE).stdout.decode())
