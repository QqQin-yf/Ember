# -*- coding: utf-8 -*-
"""Ember v3 MVP — Direct Recall 与 Presentation。

第一阶段（架构文档 §8 第一层）：只做 lexical matcher 的 exact lookup，不需要 LLM。
命中 Event 后提供完整内容，不做事件内部维度裁剪（§9）。
PresentationContext 是一次性运行时对象，不写回 Event（§10 / §11）。

Recall Cue 核心原则（momo 2026-09-30 22:52 + 2026-10-01 00:02 裁决）：
- Cue 负责提供独立的可触发点；组合不写进 Route，
  由运行时的多 Cue 共命中产生指向性。
- 不区分 must/may：指向强度不提前硬编码，
  完全由运行时聚合产生——单触点 = 弱联想，多触点共命中 = 强指向。
- Writer 不预先枚举组合（"国庆去河南""河南婚礼"这类组合 cue 是膨胀反模式）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import Event, PresentationContext, RecallTrace, RouteHit
from .store import EmberStore
from .entity import EntityStore
from .direction import DirectionMatcher
from .cue_semantic import CueVectorStore, semantic_hits


@dataclass
class RecalledEvent:
    """被想起的 Event：完整内容 + 它的原文引用 + 它为什么被想起。

    multi_cue：命中该 Event 的独立 cue 数（运行时聚合，非存储字段）。
    """
    event: Event
    route_hits: list[RouteHit] = field(default_factory=list)

    @property
    def multi_cue(self) -> int:
        return len({h.cue for h in self.route_hits})

    def to_dict(self) -> dict[str, Any]:
        return {
            "event": self.event.to_dict(),
            "raw_ref": self.event.raw_ref,
            "multi_cue": self.multi_cue,
            "why_recalled": [
                {
                    "route_id": h.route_id,
                    "cue": h.cue,
                    "matcher": h.matcher,
                    "cue_type": h.cue_type,
                }
                for h in self.route_hits
            ],
        }


@dataclass
class EntityHit:
    """一句话明确指向某个 Entity 的记录（最小闭环，运行时对象不落盘）。

    matched_via：命中用的名字/别名（baseline：exact + 子串包含，双向）。
    携带的 Event = 该 Entity 空间内所有 entity_refs 指向的 Event
    （momo 2026-10-03 裁决：进入 entity 负责的整个区域，不按 Direction 筛）；
    10-05 起 Direction 判断清晰时可缩小到该方向挂载的 Event。
    """
    entity_id: str
    name: str
    matched_via: str


class EntityRecall:
    """第二层召回（最小闭环）：输入明确指向 Entity → 进入其整个记忆区域。

    momo 2026-10-03 裁决：
    - 只做"明确指向"匹配：用已登记的 name/aliases 对输入做
      双向包含匹配（exact 命中或输入包含别名/别名包含于输入词元）。
    - 命中即带出该 Entity 空间内全部 Event；10-05 起 Direction
      判断清晰时缩小到该方向（DirectionMatcher 注入才启用）。
    - 一个 Event 可属于多个 Direction（数据层已如此），不影响本闭环。
    """

    def __init__(self, store: EmberStore, entity_store: EntityStore,
                 direction_matcher: DirectionMatcher | None = None):
        self.store = store
        self.entity_store = entity_store
        self.direction_matcher = direction_matcher

    def recall(self, input_text: str) -> tuple[list[EntityHit], list[str]]:
        """返回 (entity_hits, event_id 列表)。

        无 DirectionMatcher 时（或未命中 Entity）：行为同原最小闭环——
        命中 Entity 即带出整个区域。
        有 DirectionMatcher 且命中 Entity 时（10-05 通电）：
        - 聚焦（clear）→ 只带出该方向挂载的 Event（含 Sleep 过滤前的全集，
          过滤在 service 层统一做）；
        - 模糊（ambiguous/low_score）→ 带出整个区域 + 标记 summary_fallback，
          呈现层降级为 Entity 层 summary（摘要+id，留主动检索入口）；
        - 基础设施失败（no_vectors/embed_failed）→ 全区域回退（不引入新行为）。
        """
        lowered = input_text.lower()
        tokens = {t.strip().lower() for t in _split_tokens(input_text)}

        hits: list[EntityHit] = []
        event_ids: list[str] = []
        self.last_direction_result = None

        for entity in self.entity_store.all():
            matched_via = None
            names = [entity.name] + list(entity.aliases)
            for n in names:
                nl = n.strip().lower()
                if not nl:
                    continue
                # 明确指向：整句包含实体名，或实体名作为完整词元出现
                if nl in lowered or nl in tokens:
                    matched_via = n
                    break
            if matched_via is None:
                continue
            hits.append(EntityHit(
                entity_id=entity.entity_id,
                name=entity.name,
                matched_via=matched_via,
            ))
            # 整个区域：entity_refs 指向它的所有 Event（数据层直接读 JSONL 侧字段）
            for e in self.store.all_events():
                refs = getattr(e, "entity_refs", None) or _event_entity_refs(e.event_id)
                if entity.entity_id in refs and e.event_id not in event_ids:
                    event_ids.append(e.event_id)

        # Direction 聚焦（10-05 通电）：仅当恰好判断清晰时缩小区域；
        # 其余情形维持全区域，处置差异交给呈现层。
        if hits and self.direction_matcher is not None:
            dr = self.direction_matcher.match(hits, input_text)
            self.last_direction_result = dr
            if dr.focused and dr.top1 is not None:
                focused_dir = dr.top1[0]
                focused_ids = [
                    eid for eid in event_ids
                    if focused_dir in (
                        getattr(self.store.get_event(eid), "direction_refs", None)
                        or event_direction_refs(eid))
                ]
                # 聚焦护栏（2026-10-16）：该方向名下没有任何已挂载记忆
                # （挂载未跑/未覆盖）→ 退回全区域，聚焦绝不缩成零
                if focused_ids:
                    event_ids = focused_ids

        return hits, event_ids


def _split_tokens(text: str) -> list[str]:
    import re
    return [t for t in re.split(r"[\s，。！？、,.!?;；:：\"'（）()]+", text) if t]


# Event dataclass 尚未加 entity_refs/direction_refs 字段（内存态）。
# 最小闭环从 store 的 JSONL 原始行读挂载信息（store 不持有原始行时的兜底为空）。
# TODO(migration): Event dataclass 扩字段后删除该兜底。
_EVENT_ENTITY_REFS: dict[str, list[str]] = {}
_EVENT_DIRECTION_REFS: dict[str, list[str]] = {}


def register_event_entity_refs(events_path: str) -> None:
    """启动时从 events.jsonl 读一遍 entity_refs/direction_refs 供内存匹配。"""
    import json
    _EVENT_ENTITY_REFS.clear()
    _EVENT_DIRECTION_REFS.clear()
    try:
        with open(events_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if d.get("entity_refs"):
                    _EVENT_ENTITY_REFS[d["event_id"]] = list(d["entity_refs"])
                if d.get("direction_refs"):
                    _EVENT_DIRECTION_REFS[d["event_id"]] = list(d["direction_refs"])
    except OSError:
        pass


def _event_entity_refs(event_id: str) -> list[str]:
    return _EVENT_ENTITY_REFS.get(event_id, [])


def event_direction_refs(event_id: str) -> list[str]:
    """事件挂载的方向 id 列表（供 Direction 聚焦筛选用）。"""
    return _EVENT_DIRECTION_REFS.get(event_id, [])


class DirectRecall:
    """第一层召回：查 lexical Recall Route 索引 + Cue 语义扩展。

    管线（§2.4 匹配方式统一为 semantic，momo 2026-10-09 A 方案）：
    a) 词面 pass：分词 token exact lookup + 整句子串匹配（原行为）；
    b) 语义 pass：对词面未命中的 active lexical routes 做向量检查
       （cosine ≥ CUE_SEMANTIC_FLOOR 即命中，matcher 记 semantic）；
       embedding 失败/无向量 → 本轮降级纯词面（不引入新行为）。
    词面命中与语义命中可共存，聚合/multi_cue 一视同仁。
    """

    def __init__(self, store: EmberStore,
                 cue_vectors: CueVectorStore | None = None,
                 embed_fn=None):
        self.store = store
        # 语义段可选注入：缺省在 store.root 下找 cue_vectors.jsonl；
        # 未传且文件不存在 → 永远 no_vectors，行为退化为纯词面（原行为）
        self.cue_vectors = cue_vectors or CueVectorStore(
            getattr(store, "root", ""))
        self.embed_fn = embed_fn
        # 最近一次语义段结果（观察/记账用）：semantic_reason
        #   ok / no_vectors / embed_failed / not_requested（词面全库命中为空时无候选）
        self.last_semantic_reason = "not_requested"

    def _extract_cues(self, input_text: str) -> list[str]:
        """第一阶段 cue 提取：按空白/标点切词 + 整句短语匹配。

        注意：这是 baseline 实现，不是已证明设计（铁律）。
        """
        import re
        tokens = [t for t in re.split(r"[\s，。！？、,.!?;；:：\"'（）()]+", input_text) if t]
        return tokens

    def recall(self, input_text: str) -> PresentationContext:
        index = self.store.index_snapshot()
        cues = self._extract_cues(input_text)
        lowered = input_text.lower()

        hits: list[RouteHit] = []

        # 两路匹配（baseline 实现，不是已证明设计）：
        # a) 分词 token 对短 cue 做 exact lookup
        # b) 所有 active lexical cue 直接对整句做子串匹配（覆盖中文多字短语）
        candidates: set[str] = set()
        for cue in self._extract_cues(input_text):
            candidates.update(index.get(cue.lower(), set()))
        for cue_key, route_ids in index.items():
            if cue_key and cue_key in lowered:
                candidates.update(route_ids)

        for rid in candidates:
            route = self.store.get_route(rid)
            if route.cue.lower() not in lowered:
                continue
            hits.append(RouteHit(
                route_id=rid,
                event_id=route.event_id,
                cue=route.cue,
                matcher=route.matcher,
                cue_type=route.cue_type,
            ))

        # 语义 pass（§2.4）：对词面未命中的 active lexical routes 做向量检查。
        # embedding 失败 → 本轮降级纯词面（reason=embed_failed）；
        # 向量库为空（未重建过）→ no_vectors，行为=纯词面。
        self.last_semantic_reason = "not_requested"
        lexical_hit_ids = {h.route_id for h in hits}
        sem_candidates = [
            r for r in self.store.all_routes()
            if r.status == "active" and r.matcher == "lexical"
            and r.route_id not in lexical_hit_ids]
        if sem_candidates:
            sem_pairs, reason = semantic_hits(
                input_text, sem_candidates, self.cue_vectors,
                embed_fn=self.embed_fn)
            self.last_semantic_reason = reason
            for route, sim in sem_pairs:
                hits.append(RouteHit(
                    route_id=route.route_id,
                    event_id=route.event_id,
                    cue=route.cue,
                    matcher="semantic",
                    cue_type=route.cue_type,
                    score=sim,
                ))

        # 运行时聚合（momo 裁决）：触发点相同的命中相邻，
        # 便于后续按 Event 聚合统计 multi_cue
        anchors = sorted(hits, key=lambda h: h.cue)

        trace = RecallTrace(input_text=input_text, anchors=anchors)
        ctx = PresentationContext(
            recalled_events=[],
            recall_trace=trace,
            current_cues=list(cues),
            source_refs=[],
        )

        for hit in anchors:
            if hit.event_id not in ctx.recalled_events:
                ctx.recalled_events.append(hit.event_id)
                event = self.store.get_event(hit.event_id)
                if event.raw_ref not in ctx.source_refs:
                    ctx.source_refs.append(event.raw_ref)

        # 同批次内：multi_cue 多者优先——多触点共命中 = 更强指向（momo 裁决）
        def _strength(eid: str) -> tuple[int, ...]:
            hits = [h for h in anchors if h.event_id == eid]
            distinct = len({h.cue for h in hits})
            return (-distinct,)

        ctx.recalled_events.sort(key=_strength)
        return ctx

    def build_presentation(self, ctx: PresentationContext) -> dict[str, Any]:
        """给最终 LLM 的内容：被想起的 Event + 原文 + 为什么想起 + 触点聚合。"""
        recalled = []
        for eid in ctx.recalled_events:
            event = self.store.get_event(eid)
            hits = [h for h in ctx.recall_trace.anchors if h.event_id == eid]
            recalled.append(RecalledEvent(event=event, route_hits=hits).to_dict())
        return {
            "recalled_events": recalled,
            "recall_trace": {
                "input": ctx.recall_trace.input_text,
                "current_cues": ctx.current_cues,
                "initial_anchors": [
                    {
                        "event_id": h.event_id,
                        "route_id": h.route_id,
                        "cue": h.cue,
                        "match_type": f"{h.matcher}",
                    }
                    for h in ctx.recall_trace.anchors
                ],
            },
            "source_refs": ctx.source_refs,
            "note": (
                "以下是因当前输入被直接唤起的过往经历。"
                "每条记忆都附有 why_recalled：你是因为什么线索、通过哪条"
                " Recall Route 想起它的。请自然地利用这些记忆，"
                "不要向用户机械复述本结构。"
            ),
        }
