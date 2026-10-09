# -*- coding: utf-8 -*-
"""Ember v3 — Event turn_span（Fragment 协议的出处产出，旁路层）。

对齐背景（heart 2026-10-06 改版）：
- Fragment 协议删掉 origin_snapshot，换成 turn_span——
  片段只带出处，感受指纹 heart 现算（查它自己的 echo_digest）。
- Ember 因此唯一要提供的是：Event 覆盖的 turn 区间。

设计（架构文档 §4.1，2026-10-08 定稿）：
- 回填的是**允许断开的区间列表**，如 [(1,10),(15,20)]——
  一个 Event 的来源对话可能跳过中间的无关轮次。
- 实时写入：turn_refs 只盖粗锚点（写入那一刻的 turn）——现状不变。
- 精确回填：会话收尾 Sleep 调用①（材料=全窗口）回填每条本会话 Event
  的真实覆盖区间。本层提供回填的写入口 + 查询接口。
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

SPAN_FILE = "turn_spans.jsonl"


# ---------------------------------------------------------------------------
# 起尾句定位（quote → turn 序数；写入时 AI 附 quote_spans，此处确定性定位）
# ---------------------------------------------------------------------------

def locate_turn(quote: str, messages: list[dict]) -> int | None:
    """在对话窗口原文里逐字找 quote，返回它所在消息的 turn 序数。

    匹配策略：先精确子串；失败则空白归一后再找（换行/多空格容错，
    仍是逐字内容匹配）。找不到返回 None（调用方降级粗锚点）。
    """
    import re
    if not quote.strip():
        return None
    for m in messages:
        if m.get("role") not in ("user", "assistant"):
            continue
        content = m.get("content") or ""
        if quote in content:
            return m.get("turn")
    norm = re.sub(r"\s+", "", quote)
    if len(norm) < 4:
        return None
    for m in messages:
        if m.get("role") not in ("user", "assistant"):
            continue
        content = m.get("content") or ""
        if norm and norm in re.sub(r"\s+", "", content):
            return m.get("turn")
    return None


def spans_from_quotes(quote_spans: list[dict],
                      messages: list[dict]) -> list[tuple[int, int]]:
    """一组 (start_quote, end_quote) → 归并后的 turn 区间列表。

    单段匹配失败丢弃；起尾都能定位才算一段。空返回 = 全部失败
    （调用方降级粗锚点）。
    """
    pairs: list[tuple[int, int]] = []
    for qs in quote_spans or []:
        s = locate_turn(qs.get("start_quote", ""), messages)
        e = locate_turn(qs.get("end_quote", ""), messages)
        if s is None or e is None:
            continue
        pairs.append((min(s, e), max(s, e)))
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

    def span_of(self, event_id: str, anchor_turns: list[int] | None = None
                ) -> list[tuple[int, int]]:
        """查询 Event 的 turn 区间列表。

        已回填 → 精确区间列表（可能断开）；未回填 → 降级：
        - 有锚点列表（event.turn_refs 解析出的序数）→ [(min, max)]
        - 都没有 → []，调用方按"未知出处"处理
        """
        if event_id in self.spans:
            return self.spans[event_id]
        if anchor_turns:
            return [(min(anchor_turns), max(anchor_turns))]
        return []

    def span_of_event(self, event) -> list[tuple[int, int]]:
        """便捷：直接从 Event 对象解析锚点降级。"""
        import re
        turns = []
        for ref in (getattr(event, "turn_refs", None) or []):
            m = re.search(r"(\d+)$", str(ref))
            if m:
                turns.append(int(m.group(1)))
        return self.span_of(getattr(event, "event_id", ""), turns)

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
