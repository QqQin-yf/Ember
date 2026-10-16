# -*- coding: utf-8 -*-
"""Ember v3 — 原文拉取客户端（去原文化 2026-10-14，拍板①：按需向后端拉）。

职责（唯一）：向「原文提供接口」按 message_id 区间拉对话原文，用完即弃，
不落盘。过渡期提供方 = heart-chat 只读端点（/api/source）；Deliz 新后端
上线后只换 EMBER_SOURCE_URL，客户端与调用方零改动。

降级哲学（与旁路钩子同款）：
- EMBER_SOURCE_URL 未配置 → unavailable（调用方按「语料不可用」处理）；
- 网络失败/超时/非 200 → unavailable，绝不重试轰炸；
- 拉到空区间 → 返回空列表（合法：区间内没有消息）。

调用方与降级语义（transcripts 退役时的四个消费方）：
- 双盲核对 / key_quotes 逐字核对：语料不可用 → 如实拒绝写入（quote
  无法核对就不该打无出处之账）；
- read_memory_source：不可用 → not_archived 类回执；
- Sleep 管线材料：不可用 → 跳过该项（有现成 skip 路径）。

配置：
- EMBER_SOURCE_URL    原文接口基址（如 http://heart-chat:3000）
- EMBER_SOURCE_TOKEN  Bearer 鉴权（可选，与提供方约定一致）
- EMBER_SOURCE_TIMEOUT  超时秒数（默认 8）
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

SOURCE_URL = os.environ.get("EMBER_SOURCE_URL", "")
SOURCE_TOKEN = os.environ.get("EMBER_SOURCE_TOKEN", "")
SOURCE_TIMEOUT = float(os.environ.get("EMBER_SOURCE_TIMEOUT", "8"))


class SourceUnavailable(Exception):
    """原文提供方不可用（未配置/网络失败/非 200）。调用方按各自降级路径处理。"""


def configured() -> bool:
    return bool(SOURCE_URL)


def fetch_source_range(session_id: str, from_id: int,
                       to_id: int) -> list[dict[str, Any]]:
    """拉 [from_id, to_id] 区间的对话原文（后端消息行，含被截断的）。

    返回 [{message_id, role, content}]（按 message_id 升序）；
    提供方未配置/不可达 → SourceUnavailable。
    content 原样返回（不做任何截断——调用方自行限流）。
    """
    if not SOURCE_URL:
        raise SourceUnavailable("EMBER_SOURCE_URL 未配置")
    base = SOURCE_URL.rstrip("/")
    url = (f"{base}/api/source?session_id={urllib.parse.quote(session_id)}"
           f"&from={int(from_id)}&to={int(to_id)}")
    req = urllib.request.Request(url)
    if SOURCE_TOKEN:
        req.add_header("Authorization", f"Bearer {SOURCE_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=SOURCE_TIMEOUT) as resp:
            if resp.status != 200:
                raise SourceUnavailable(f"source endpoint {resp.status}")
            data = json.loads(resp.read().decode("utf-8"))
    except SourceUnavailable:
        raise
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError) as exc:
        raise SourceUnavailable(f"原文拉取失败: {exc}") from exc
    rows = data.get("messages")
    if not isinstance(rows, list):
        raise SourceUnavailable("source 响应缺 messages 数组")
    out: list[dict[str, Any]] = []
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get("message_id"), int):
            continue
        row = {"message_id": r["message_id"],
               "role": r.get("role"),
               "content": r.get("content") or ""}
        # created_at 透传（观察事件的原文诞生时刻用；后端没给就算了）
        if r.get("created_at") is not None:
            row["created_at"] = r["created_at"]
        out.append(row)
    return out
