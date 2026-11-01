# -*- coding: utf-8 -*-
"""Ember v3 MVP — 核心数据模型。

铁律（来自架构文档第 2 节）：
Event 本身不保存 activation / brightness / current relevance /
propagation weight / current rank / current importance。
任何运行时状态不得写回 Event。

第一阶段只实现 lexical matcher（semantic matcher 留待下一阶段）。

momo 2026-10-01 00:02 裁决：取消 must/may 区分。
RecallRoute 不再有 mode 字段；指向强度不提前硬编码，
由运行时多 Cue 共命中聚合产生。
"""
from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any

# 禁止写进 Event 的运行时字段（架构文档 §2 + §11）
FORBIDDEN_EVENT_FIELDS = {
    "activation", "brightness", "current_relevance",
    "propagation_weight", "current_rank", "current_importance",
    "weight", "score",
}

ALLOWED_MATCHERS = {"lexical", "semantic"}
ALLOWED_CUE_TYPES = {"lexical", "conceptual", "relational", "situational"}
ALLOWED_ROUTE_STATUS = {"active", "retired"}

_id_counter = itertools.count(1)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class Event:
    """一段实际发生过的经历。内容整体可见，不做事件内部维度裁剪。"""
    event_id: str
    version_id: str
    # evidence —— 只保存事实来源
    source_ref: str
    raw_ref: str
    event_time: str
    ingest_time: str
    turn_refs: list[str] = field(default_factory=list)
    # content —— 完整内容
    summary: str = ""
    core: str = ""
    significance: int = 5
    kind: str = "daily"
    focus: str = ""
    due: str | None = None
    status: str = "open"
    log: list[dict[str, Any]] = field(default_factory=list)
    # Existing maintenance annotation, not a model-authored content field.
    change: str = ""
    entity_refs: list[str] = field(default_factory=list)
    merged_into: str = ""
    # provenance
    provenance_refs: list[str] = field(default_factory=list)
    # origin —— 事件出身："" = 实时写入（对话中模型自主触发）；
    # "observe" = 观察式记忆（sleep 夜批从未覆盖区间提炼的信息点，
    # 无 cue、语义召回走附加席）；被判联印证（判出 relation）后
    # 改写为 "affirmed"（2026-10-16 设计夜定稿）。
    origin: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Event":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(d) - known
        if unknown & FORBIDDEN_EVENT_FIELDS:
            raise ValueError(
                f"禁止把运行时字段写进 Event: {sorted(unknown & FORBIDDEN_EVENT_FIELDS)}")
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class RecallRoute:
    """Event 自己的记忆触点：什么东西出现时，这个 Event 应该被想起。

    momo 2026-10-01 裁决：不再区分 must/may。
    每个 cue 是"最小不可拆分的独立语义单元"，
    指向强度由运行时多触点共命中聚合产生，不提前硬编码。
    """
    route_id: str
    event_id: str
    matcher: str                   # lexical / semantic —— 匹配方式（第一阶段仅 lexical）
    cue_type: str                  # lexical / conceptual / relational / situational
    cue: str                       # 线索本体
    status: str = "active"         # active / retired
    evidence_refs: list[str] = field(default_factory=list)
    provenance_refs: list[str] = field(default_factory=list)

    def __post_init__(self):
        if self.matcher not in ALLOWED_MATCHERS:
            raise ValueError(f"matcher 必须是 {ALLOWED_MATCHERS}，得到 {self.matcher!r}")
        if self.cue_type not in ALLOWED_CUE_TYPES:
            raise ValueError(f"cue_type 必须是 {ALLOWED_CUE_TYPES}，得到 {self.cue_type!r}")
        if self.status not in ALLOWED_ROUTE_STATUS:
            raise ValueError(f"status 必须是 {ALLOWED_ROUTE_STATUS}，得到 {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RecallRoute":
        # 旧数据兼容：忽略已废弃的 mode 字段（momo 2026-10-01 取消 must/may）
        d = {k: v for k, v in d.items() if k != "mode"}
        return cls(**d)


@dataclass
class RouteHit:
    """一次命中锚点（lexical 词面 / semantic 语义）。"""

    route_id: str
    event_id: str
    cue: str
    matcher: str
    cue_type: str
    # 语义命中时的 cosine 相似度（2026-10-19 透传：判定层要用真分数，
    # 不再拿「存在即过值」凑合）；词面命中为 None——词面是子串精确
    # 匹配，没有相似度概念。
    score: float | None = None


@dataclass
class RecallTrace:
    """为什么想起 —— recall 过程的完整痕迹（一次性运行时对象，不写回 Event）。"""
    input_text: str
    anchors: list[RouteHit] = field(default_factory=list)


@dataclass
class PresentationContext:
    """一次召回的呈现上下文。一次性对象，不写回 Event（架构文档 §10）。"""
    recalled_events: list[str] = field(default_factory=list)
    recall_trace: RecallTrace | None = None
    current_cues: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
