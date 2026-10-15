# -*- coding: utf-8 -*-
"""Ember v3 — executor 配置持久化与公共 LLM 调用层。

这个文件是干什么的：
  1. 读 executors.json（各 LLM 任务点的默认 engine 配置）；
  2. 提供公共 call_llm（glm API / qwen3 本地 docker 两个 engine），
     供三个离线脚本（sleep_v2 / split_directions / mount_directions）
     引用，取代原先各脚本里的重复实现。

被谁调用：
  - scripts/sleep_v2.py（任务点 judge，调用②裁决与归属）
  - scripts/split_directions.py（任务点 split，Direction 划分）
  - scripts/mount_directions.py（任务点 mount，Direction 挂载）
  - scripts/sleep_run.py（judge 经由 sleep_v2 间接使用；
    summary = Entity summary 重算，裁决后独立调用）

executors.json 约定（momo 2026-10-09 拍板）：
  - 位置：ember-v3-mvp 根目录（BASE），无密钥、入 git；
  - 形态：{"judge": "glm", "split": "glm", "mount": "glm", "summary": "glm"}
    键 = LLM 任务点；值 = engine 名（glm / qwen3）；
  - 谁写：momo 手工编辑；谁读：离线脚本启动时（load_engine）；
  - --engine 命令行参数保留为单次覆盖（显式给了就用它）；
  - 配置文件缺失/任务点缺项/值非法 → 回落 glm（与历史默认行为一致）。

"外包"口径（momo 2026-10-09）：凡不带记忆、上下文执行的 LLM 调用
都归类为外包——glm API 与本地 qwen3 均为外包，与 Chat Model（本人，
带完整对话上下文）相对。
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.request

EXECUTORS_FILE = "executors.json"

ALLOWED_ENGINES = ("glm", "qwen3")
KNOWN_TASKS = ("judge", "split", "mount", "summary", "structure")

# 各任务点的调用差异（原先散在三个脚本里的常量，集中到这张表）：
#   model_env   glm 模型的环境变量名（回退 EMBER_SLEEP_MODEL，再回退 glm-4.7）
#   temperature 采样温度
#   timeout     HTTP 超时秒数
TASK_SPECS: dict[str, dict] = {
    "judge": {"model_env": "EMBER_SLEEP_MODEL", "temperature": 0.2, "timeout": 120},
    "split": {"model_env": "EMBER_SPLIT_MODEL", "temperature": 0.3, "timeout": 180},
    "mount": {"model_env": "EMBER_MOUNT_MODEL", "temperature": 0.2, "timeout": 120},
    "summary": {"model_env": "EMBER_SUMMARY_MODEL", "temperature": 0.3, "timeout": 120},
    "structure": {"model_env": "EMBER_STRUCTURE_MODEL", "temperature": 0.2,
                  "timeout": 180},
}


def load_executors(base_dir: str) -> dict[str, str]:
    """读 executors.json（base_dir 下），返回 任务点 → engine。

    缺文件/坏 JSON/非法值 → 该项回落 glm；多余的任务点忽略。
    只做读取与清洗，不做任何写入。
    """
    path = os.path.join(base_dir, EXECUTORS_FILE)
    raw: dict = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError):
            raw = {}
    out: dict[str, str] = {}
    for task in KNOWN_TASKS:
        v = raw.get(task)
        out[task] = v if v in ALLOWED_ENGINES else "glm"
    return out


def load_engine(base_dir: str, task: str, override: str | None = None) -> str:
    """单个任务点的 engine 解析：--engine 显式覆盖 > executors.json > glm。"""
    if override in ALLOWED_ENGINES:
        return override
    return load_executors(base_dir).get(task, "glm")


def call_llm(system: str, user: str, engine: str, task: str = "judge") -> str:
    """公共 LLM 调用。engine=glm：智谱 OpenAI-compatible API；
    engine=qwen3：docker exec 进 ombre-ollama（本地兜底）。

    task 决定模型环境变量/temperature/timeout（TASK_SPECS）。
    失败抛异常（urllib.error / subprocess 异常 / ValueError），由调用方重试。
    """
    if engine == "qwen3":
        full = system + "\n\n" + user
        r = subprocess.run(
            ["docker", "exec", "-i", "ombre-ollama", "ollama", "run",
             "qwen3:1.7b", "/no_think"],
            input=full.encode("utf-8"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        return r.stdout.decode("utf-8", errors="replace")
    if engine == "glm":
        spec = TASK_SPECS.get(task, TASK_SPECS["judge"])
        api_key = os.environ.get("ZHIPU_API_KEY", "")
        base = os.environ.get("EMBER_BASE_URL",
                              "https://open.bigmodel.cn/api/paas/v4")
        model = os.environ.get(
            spec["model_env"],
            os.environ.get("EMBER_SLEEP_MODEL", "glm-4.7"))
        body = json.dumps({
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": spec["temperature"],
        }).encode("utf-8")
        req = urllib.request.Request(
            base.rstrip("/") + "/chat/completions", data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"})
        with urllib.request.urlopen(req, timeout=spec["timeout"]) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    raise ValueError(f"未知 engine: {engine}")
