# -*- coding: utf-8 -*-
"""Ember v3 — Event turn_span（Fragment 协议的出处产出，旁路层）。

对齐背景（heart 2026-10-06 改版）：
- Fragment 协议删掉 origin_snapshot，换成 turn_span——
  片段只带出处，感受指纹 heart 现算（查它自己的 echo_digest）。
- Ember 因此唯一要提供的是：Event 覆盖的 turn 区间。

设计（架构文档 §4.1，2026-10-08 定稿）：
- 回填的是**允许断开的区间列表**，如 [(1,10),(15,20)]——
  一个 Event 的来源对话可能跳过中间的无关轮次。
- 实时写入：turn_refs 盖出生图章（写入那一刻的 turn，仅出处观察字段，
  不作为时间坐标使用）。
- 精确区间唯一来源 = 写入时实时定位（2026-10-12 裁决：定位语料=持久
  存档∪内存，见 service._quote_corpus；原设计的「Sleep 调用①全窗口
  回填」移除——不建第二真相源；turn_refs 锚点代答同裁决移除——出生
  时刻不冒充经历时刻）。定位失败的句对整对拒绝写入（双盲）；半有效
  取就近边界（2026-10-17 momo 改定：单端定位往后/往前 10 条消息，
  首尾同句 ±3 轮——不再拉到会话尽头，过宽的保证边界会把半有效事件
  整段判进简报现场）。span_of 查询未回填 → []，调用方按"未知出处"
  处理
  （梦选材跳过、翻原文 not_archived，均有现成路径）。
- 本层提供定位（spans_from_quotes）+ 写入口（fill）+ 查询接口。
- 回填产物落旁路文件 turn_spans.jsonl（append-only，可重建），
  不写进 Event 本体（铁律：Event 不存运行时/派生状态）。
- 未回填的事件查询时降级：用粗锚点给出 [min,max] 单点区间。
- 兼容：旧文件里的单区间行（turn_start/turn_end）照常读入，
  转成单元素区间列表。

heart 的 turn_span 语义：Fragment 带的是 ember 侧 turn-N 区间；
heart 拿映射账本（turn_ledger）换算成自己的 turn_id 区间后
查 echo_digest。两侧坐标系的换算在 heart 侧完成。

文件行格式（v2，区间列表）：
  {"event_id", "spans": [[s1,e1],[s2,e2]], "filled_by", "recorded_at"}
兼容读入（v1，单区间）：
  {"event_id", "turn_start", "turn_end", "filled_by", "recorded_at"}
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .source_paths import QuoteSourceError

SPAN_FILE = "turn_spans.jsonl"


# ---------------------------------------------------------------------------
# 起尾句定位（quote → turn 序数；写入时 AI 附 quote_spans，此处确定性定位）
# ---------------------------------------------------------------------------

# 半有效句对的就近边界（2026-10-17 momo 改定）：缺失端不再拉到会话
# 尽头（过宽的保证边界会把半有效事件整段判进简报现场），改为从已
# 定位端往后/往前数这么多条消息（不足有多少取多少）。
FALLBACK_SPAN_MESSAGES = 10
# 首尾同一句时的就近上下文：±3 轮（与 read_memory_source 的单句
# 扩展同口径，前后不足就有多少取多少）。
SAME_QUOTE_CONTEXT_TURNS = 3


def _conversation(messages: list[dict]) -> list[dict]:
    """定位语料：user/assistant 且带合法 turn 序数的消息，保持原顺序。"""

    return [m for m in messages
            if m.get("role") in ("user", "assistant")
            and isinstance(m.get("turn"), int)]


def _locate_index(quote: str, corpus: list[dict], *, field: str = "quote_spans") -> int | None:
    """quote 在语料中的消息下标。

    先精确子串；失败后仅保留文字和数字，忽略格式、emoji 和空白。
    容错匹配必须唯一，歧义报错；文字仍须连续对应，不跳过任何文字。
    返回的是原始消息下标，不改写原文。找不到返回 None。
    """
    if not quote.strip():
        return None
    for i, m in enumerate(corpus):
        if quote in (m.get("content") or ""):
            return i
    norm = "".join(c for c in quote if c.isalnum())
    if not norm:
        return None
    candidates = []
    for i, m in enumerate(corpus):
        content = m.get("content") or ""
        if norm in "".join(c for c in content if c.isalnum()):
            candidates.append(i)
    if len(candidates) > 1:
        raise QuoteSourceError("quote_ambiguous", field=field,
                               candidate_count=len(candidates))
    return candidates[0] if candidates else None


def locate_turn(quote: str, messages: list[dict]) -> int | None:
    """在对话窗口原文里逐字找 quote，返回它所在消息的 turn 序数。

    先精确子串，再忽略非文字数字字符匹配；容错歧义抛出可诊断错误。
    找不到返回 None。
    """

    corpus = _conversation(messages)
    idx = _locate_index(quote, corpus)
    return corpus[idx]["turn"] if idx is not None else None


def spans_from_quotes(quote_spans: list[dict],
                      messages: list[dict]) -> list[tuple[int, int]]:
    """一组 (start_quote, end_quote) → 归并后的 turn 区间列表。

    定位与边界（2026-10-17 momo 改定）：
    - 首尾都定位 → 精确区间；落在同一句 → 就近上下文 ±3 轮
      （与 read_memory_source 单句扩展同口径，不足有多少取多少）；
    - 只有首句 → 从首句所在消息往后数 10 条消息（不足到语料末尾）；
    - 只有尾句 → 从尾句所在消息往前数 10 条消息（不足到语料开头）；
    - 两端都定位不到 → 整对放弃（无任何真实坐标）。
    半有效仍是包含真实跨度的宽区间，只是从「拉到会话尽头」收窄为
    「就近 10 条消息」。空返回 = 全部放弃（调用方降级粗锚点）。
    """
    corpus = _conversation(messages)
    if not corpus:
        return []
    turns = [m["turn"] for m in corpus]
    lo_turn, hi_turn = min(turns), max(turns)

    pairs: list[tuple[int, int]] = []
    for i, qs in enumerate(quote_spans or []):
        s_idx = _locate_index(qs.get("start_quote", ""), corpus,
                              field=f"quote_spans[{i}].start_quote")
        e_idx = _locate_index(qs.get("end_quote", ""), corpus,
                              field=f"quote_spans[{i}].end_quote")
        if s_idx is None and e_idx is None:
            continue
        if s_idx is None:
            # 只有尾句：往前数 10 条消息（不足到语料开头）
            s_idx = max(0, e_idx - FALLBACK_SPAN_MESSAGES)
        elif e_idx is None:
            # 只有首句：往后数 10 条消息（不足到语料末尾）
            e_idx = min(len(corpus) - 1, s_idx + FALLBACK_SPAN_MESSAGES)
        if s_idx == e_idx:
            # 首尾同一句 → ±3 轮就近上下文（不足有多少取多少）
            t = turns[s_idx]
            pairs.append((max(lo_turn, t - SAME_QUOTE_CONTEXT_TURNS),
                          min(hi_turn, t + SAME_QUOTE_CONTEXT_TURNS)))
        else:
            pairs.append((min(turns[s_idx], turns[e_idx]),
                          max(turns[s_idx], turns[e_idx])))
    if not pairs:
        return []
    pairs.sort()
    merged = [pairs[0]]
    for s, e in pairs[1:]:
        if s <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _norm_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """规范化：去重叠/相邻合并、各自内部有序、整体按起点排序。

    [(1,10),(15,20)] 保留断开；[(1,10),(5,12)] 合并为 (1,12)。
    turn_end < turn_start 视为数据错误。
    """
    cleaned = []
    for s, e in spans:
        if e < s:
            raise ValueError(f"turn_span invalid: {s} > {e}")
        cleaned.append((s, e))
    cleaned.sort()
    merged: list[tuple[int, int]] = []
    for s, e in cleaned:
        if merged and s <= merged[-1][1] + 1:  # 重叠或相邻 → 合并
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


@dataclass
class TurnSpanStore:
    """Event 的 turn 覆盖区间列表（旁路文件 + 粗锚点降级）。"""
    root: str
    # event_id -> [(turn_start, turn_end), ...]（ember 侧序数，允许断开）
    spans: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    provenance: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        p = os.path.join(self.root, SPAN_FILE)
        if not os.path.exists(p):
            return
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                eid = d.get("event_id") or ""
                if not eid:
                    continue
                if "spans" in d:  # v2 区间列表
                    pairs = [(int(s), int(e)) for s, e in d["spans"]]
                else:             # v1 单区间兼容
                    s, e = d.get("turn_start"), d.get("turn_end")
                    if not (isinstance(s, int) and isinstance(e, int)):
                        continue
                    pairs = [(s, e)]
                self.spans[eid] = _norm_spans(pairs)
                self.provenance[eid] = d.get("filled_by") or "unknown"

    def _path(self) -> str:
        return os.path.join(self.root, SPAN_FILE)

    def fill(self, event_id: str, spans: list[tuple[int, int]],
             filled_by: str = "sleep-closing") -> dict[str, Any]:
        """回填一条 Event 的真实覆盖区间列表（append-only 留痕）。

        spans 为空视为数据错误——回填的意义就是给出出处。
        """
        if not spans:
            raise ValueError(f"turn_span empty for {event_id}")
        merged = _norm_spans(spans)
        entry = {
            "event_id": event_id,
            "spans": [list(p) for p in merged],
            "filled_by": filled_by,
            "recorded_at": _now_iso(),
        }
        with open(self._path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.spans[event_id] = merged
        self.provenance[event_id] = filled_by
        return entry

    def span_of(self, event_id: str) -> list[tuple[int, int]]:
        """查询 Event 的 turn 区间列表。

        已回填 → 精确区间列表（可能断开）；未回填 → []（无真实坐标，
        调用方按"未知出处"处理——2026-10-12 裁决移除 turn_refs 锚点
        代答：出生时刻不冒充经历时刻）。
        """
        return list(self.spans.get(event_id, []))

    def span_of_event(self, event) -> list[tuple[int, int]]:
        """便捷：按 event_id 查区间；无回填 → []。"""
        return self.span_of(getattr(event, "event_id", ""))

    def covers(self, event_id: str, turn: int) -> bool:
        """turn 是否落在该 Event 的任一区间内（候选组装的交集判断）。"""
        return any(s <= turn <= e
                   for s, e in self.spans.get(event_id, []))

    def filled_ids(self) -> list[str]:
        return sorted(self.spans)

    def to_dict(self) -> dict[str, Any]:
        return {
            "filled_count": len(self.spans),
            "spans": {k: [list(p) for p in v]
                      for k, v in sorted(self.spans.items())},
        }
