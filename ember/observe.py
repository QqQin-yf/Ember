# -*- coding: utf-8 -*-
"""Ember v3 — 观察式记忆（2026-10-16 设计夜定稿，④observe）。

观察 = 从「未被事件 span 覆盖」的对话区间里提炼的**信息点**：
只收行为模式（怎么做事、怎么相处），不收个人档案（前端的地盘）、
不收具体事件（正式记忆的活）。无 cue、只用语义召回、附加席呈现；
被 sleep 判联印证（判出 relation）即转正（origin: observe → affirmed）。

本模块只放账本与纯函数；编排（拉取→提炼→落库→合并）在
service.run_observe_pass / service._merge_observations——它们要摸
store/ledger/spans/vectors 一大家子，放 service 与现有夜批同构。

管线（momo 拍板）：
  sleep 收尾：查游标 → 攒够 OBSERVE_MIN_UNCOVERED 条未覆盖消息
    → 拉区间原文 → 第三方 LLM 提炼（不硬挖，0-N 条）
    → 落库 + fill turn_span + 当场建向量 + 进 link pending
  判联（现成管线零改动）：relation → 转正；neighbor/none → 放着
  夜批合并：观察向量比对 → 高相似聚 → LLM 判定并写合并版
    → 旧观察标 superseded（复用现成修正机制，召回自动不再带出）
"""
from __future__ import annotations

import json
import os
from typing import Any

CURSOR_FILE = "observe_cursor.json"

# 拍板常量（2026-10-16）：门槛 = 一天的量（momo 日均 ~300 条），
# 上限防爆窗口；相似阈值保守起高——合并宁可漏不可错。
MIN_UNCOVERED = 300
MAX_BATCH = 400
MAX_OUTPUT = 8            # 单次提炼产出的兜底上限（防模型刷屏）
SIM_THRESHOLD = 0.90
MAX_MERGE_GROUP = 4       # 单组送判的观察条数上限
HIT_FLOOR = 0.35          # 附加席命中下限（宽松取向：被召回才谈印证）


class ObserveCursor:
    """观察游标：每个会话已处理到的后端 seq（message_id）。

    状态文件 observe_cursor.json（单对象，非 append-only——它就是
    一个指针，语义 = 「此前（含）都已检查过」）。游标只在**成功
    提炼**后推进：不够门槛 / LLM 失败 / 拉取失败都原地不动，滚到
    下一轮 sleep 攒批。
    """

    def __init__(self, root: str):
        self.root = root
        self._pos: dict[str, int] = {}
        self._load()

    def _path(self) -> str:
        return os.path.join(self.root, CURSOR_FILE)

    def _load(self) -> None:
        p = self._path()
        if not os.path.exists(p):
            return
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            return
        pos = d.get("positions")
        if isinstance(pos, dict):
            self._pos = {k: int(v) for k, v in pos.items()
                         if isinstance(v, int)}

    def get(self, session_id: str) -> int:
        return self._pos.get(session_id, 0)

    def set(self, session_id: str, seq: int) -> None:
        """推进游标（只前进不后退）。"""
        if seq <= self._pos.get(session_id, 0):
            return
        self._pos[session_id] = int(seq)
        tmp = self._path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"positions": self._pos}, f, ensure_ascii=False)
        os.replace(tmp, self._path())


def dialogue_lines(messages: list[dict[str, Any]]) -> str:
    """后端消息行 → 提炼用对话文本：每条一块，行首说话人标注。

    归属绝对以行首标注为准（防张冠李戴的锚）；assistant 统一
    写作「assistant」。角色值域外（异常行）原样标「?」。
    """
    blocks = []
    for m in messages:
        role = m.get("role")
        who = {"user": "user", "assistant": "assistant", "ai": "assistant"}.get(
            str(role), "?")
        blocks.append(f"{who}：{m.get('content', '')}")
    return "\n\n".join(blocks)


def verify_quotes(quotes: list, messages: list[dict[str, Any]]) -> list[dict]:
    """引文逐字核对：text 必须在对应说话人的消息里逐字出现。

    返回保留的引文（speaker 归一为 user/ai，对齐 Event.key_quotes
    的 schema）；对不上的整条丢弃——观察的证据必须是真话。
    """
    # 按说话人聚合原文，子串匹配（一个字都不能改）
    corpus: dict[str, str] = {}
    for m in messages:
        role = str(m.get("role"))
        spk = "user" if role == "user" else "ai"
        corpus[spk] = corpus.get(spk, "") + "\n" + str(m.get("content", ""))
    kept: list[dict] = []
    for q in quotes or []:
        if not isinstance(q, dict):
            continue
        spk = str(q.get("speaker", "")).strip()
        spk = {"assistant": "ai"}.get(spk, spk)
        text = str(q.get("text", "")).strip()
        if spk not in ("user", "ai") or not text:
            continue
        if text in corpus.get(spk, ""):
            kept.append({"speaker": spk, "text": text})
        if len(kept) >= 3:
            break
    return kept
