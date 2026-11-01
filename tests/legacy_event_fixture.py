"""Explicit legacy Event-rendering fixture for historical protocol regressions.

These tests protect frozen Event responses, source bindings, expiry and old
renderers. Production EmberService hooks use raw slices; this subclass is never
imported by application code. New automatic recall tests use EmberService itself.
"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any
from ember.service import EmberService, Session, _event_entity_of, _event_ingest_ts, _now_iso
from ember.models import PresentationContext
from ember.recall import DirectRecall, EntityRecall
from ember.direction import DirectionMatcher, DirectionVectorStore

def select_recall_units(recalled_events: list[str], anchors: list,
                        entity_hits: list, resolved_brought: list,
                        store, seats: int = 3,
                        now: datetime | None = None):
    """多源共证与名额（2026-10-15 设计夜定稿）——召回注入的选取器。

    可及方式按「源」计数（momo：路按源数不按锚数）：cue（lexical/
    semantic 同属 cue 源）/ entity（direction 并入实体路）。
    主席资格（三选一）：
      - 双源共证：route_count ≥ 2
      - 修正者顶上：relation 路不受限（跟随命中即共证）
      - 新生豁免：写入 48h 内（豁免新生在主席内限 2 席，超额落池）
    单源 → 弱信号池：席位富余才补（补位标 weak=True），没补上的落选。
    排序键 = (-源数, -cue数, -新旧)：锚数（去重 cue 文本数，修正者继承/
    实体块取成员并集）只做排序优先级——弱池内多 cue 命中优先补位，
    同源数的主席单元多 cue 者前；资格永远只看源数（momo 10-16 拍板）。
    实体块块级资格（块内并集源数 ≥2），整块 1 席。
    返回 (selected_units, stats)：
      stats = {"dropped_by_seats": 总落选, "weak_pooled": 弱信号未补,
               "strong_dropped": 共证却被挤出（≥2 源，驱动注入尾部提示）}
    selected 里弱信号补位的 unit 带 weak=True。
    """
    FRESH_HOURS = 48
    FRESH_SEATS_MAX = 2

    now = now or datetime.now(timezone.utc).astimezone()
    entity_hit_ids = {h.entity_id for h in entity_hits}
    hit_matchers: dict[str, set[str]] = {}
    hit_cues: dict[str, set[str]] = {}
    for h in anchors:
        hit_matchers.setdefault(h.event_id, set()).add(h.matcher)
        hit_cues.setdefault(h.event_id, set()).add(h.cue)
    resolved_src = dict(resolved_brought or [])  # 修正者 -> 被修正者

    def _hits_union(eid: str) -> set[str]:
        """命中该事件的 matcher 并集；修正者继承被修正者的命中。"""
        src = resolved_src.get(eid)
        return (hit_matchers.get(eid, set())
                | (hit_matchers.get(src, set()) if src else set()))

    def _cues_of(eid: str) -> set[str]:
        """命中该事件的 cue 文本集；修正者继承被修正者的 cue。"""
        src = resolved_src.get(eid)
        return (hit_cues.get(eid, set())
                | (hit_cues.get(src, set()) if src else set()))

    def _sources(eid: str) -> set[str]:
        """点亮该事件的可及源（cue/entity，同源合并）。"""
        s = set()
        if _hits_union(eid):
            s.add("cue")
        if _event_entity_of(store, eid) in entity_hit_ids:
            s.add("entity")
        return s

    def _is_fresh(eid: str) -> bool:
        ts = _event_ingest_ts(store, eid)
        if not ts:
            return False
        return 0 <= (now.timestamp() - ts) / 3600.0 < FRESH_HOURS

    def _unit_key(u: dict):
        # 锚数（cue 文本数）只做排序优先级，永不影响资格（momo 10-16 拍板）：
        # 资格看源数（防锚数灌水），排序看锚数（多 cue 共撞=更相关）
        return (-u["route_count"], -u.get("cue_count", 0),
                -max(_event_ingest_ts(store, e) for e in u["event_ids"]))

    units: list[dict] = []
    block_of_entity: dict[str, dict] = {}
    for eid in recalled_events:
        ent = _event_entity_of(store, eid)
        if ent is not None and ent in entity_hit_ids:
            blk = block_of_entity.get(ent)
            if blk is None:
                blk = {"kind": "entity_block", "entity_id": ent,
                       "event_ids": [], "route_count": 0, "group": 1}
                units.append(blk)
                block_of_entity[ent] = blk
            blk["event_ids"].append(eid)
        else:
            srcs = _sources(eid)
            union = _hits_union(eid)
            group = 1 if ("entity" in srcs
                          or "lexical" in union) else 2
            units.append({"kind": "event", "event_ids": [eid],
                          "route_count": len(srcs), "group": group,
                          "cue_count": len(_cues_of(eid)),
                          "resolved": eid in resolved_src,
                          "fresh": _is_fresh(eid)})
    for blk in units:
        if blk["kind"] != "entity_block":
            continue
        all_srcs: set[str] = set()
        all_cues: set[str] = set()
        for e in blk["event_ids"]:
            all_srcs |= _sources(e)
            all_cues |= _cues_of(e)
        blk["route_count"] = len(all_srcs)
        blk["cue_count"] = len(all_cues)
        blk["fresh"] = any(_is_fresh(e) for e in blk["event_ids"])

    # ---- 资格判定与席位 ----
    def _qualify(u: dict) -> str:
        if u["kind"] == "event" and u.get("resolved"):
            return "resolved"
        if u["route_count"] >= 2:
            return "multisource"
        if u.get("fresh"):
            return "fresh"
        return "weak"

    for u in units:
        u["_why"] = _qualify(u)
    qualified = [u for u in units if u["_why"] != "weak"]
    weak = [u for u in units if u["_why"] == "weak"]
    g1 = sorted((u for u in qualified if u["group"] == 1), key=_unit_key)
    g2 = sorted((u for u in qualified if u["group"] == 2), key=_unit_key)
    selected = list(g1[:seats])
    selected += g2[:max(0, seats - len(selected))]
    weak = sorted(weak, key=_unit_key)

    # 新生限额：主席内豁免新生超额的挪到弱信号池头部（优先回补）
    fresh_sel = [u for u in selected if u["_why"] == "fresh"]
    overflow = fresh_sel[FRESH_SEATS_MAX:]
    if overflow:
        selected = [u for u in selected if u not in overflow]
        weak = overflow + weak

    # 弱信号补位（席位富余才补）
    still = seats - len(selected)
    take, weak_pooled = weak[:still], weak[still:]
    for u in take:
        u["weak"] = True
    selected += take

    strong_dropped = len([u for u in qualified if u not in selected
                          and (u["route_count"] >= 2 or u.get("resolved"))])
    stats = {"dropped_by_seats": len(units) - len(selected),
             "weak_pooled": len(weak_pooled),
             "strong_dropped": strong_dropped}
    return selected, stats


class LegacyEventService(EmberService):
    def __init__(self, store, *args, **kwargs):
        super().__init__(store, *args, **kwargs)
        self.direction_matcher = DirectionMatcher(self.entity_store, DirectionVectorStore(store.root))

    def legacy_event_recall_pipeline(self, session: Session, turn: int,
                            user_text: str,
                            context_start_id: int | None = None,
                            deferred_entries: list[dict] | None = None,
                            context_message_ids: list[int] | None = None
                            ) -> dict[str, Any]:
        """保留的 Event 召回：Cue/Entity 双路 → Sleep 过滤 → 窗口过滤
        → Relation 展开 → 召回记账 → 前缀块构建。

        普通轮前与显式历史检索共用此路；观察附加席已删除。
        记账失败不阻断（旁路只观察）。返回：
        {prefix, recalled_count, entity_recalled, entity_names,
         filtered_by_sleep, filtered_by_window, resolved_brought,
         direction_focused, direction_reason, summary_fallback,
         semantic_matched, semantic_reason}

        窗口过滤（2026-10-14 去原文化，图第 2 点拍板）：context_start_id
        = 当前窗口起点（后端 message_id），经 turn_ledger 换算成起点
        turn 后，来源 turn 区间全部 ≥ 起点的候选滤掉——原文就在现场，
        记忆只负责窗口外的事。无来源坐标（span 未回填）保守保留。
        """
        direct_recaller = DirectRecall(self.store)
        recalled_ctx = direct_recaller.recall(user_text)
        semantic_matched = sum(
            1 for h in recalled_ctx.recall_trace.anchors
            if h.matcher == "semantic")

        # Entity 通路（最小闭环 + Direction 聚焦，10-05 通电）：
        # 输入明确指向 Entity → 判断方向 → 聚焦该方向 / 或全区域+summary 回退
        entity_recaller = EntityRecall(
            self.store, self.entity_store,
            direction_matcher=self.direction_matcher)
        entity_hits, entity_event_ids = entity_recaller.recall(user_text)
        dir_result = entity_recaller.last_direction_result
        direction_reason = dir_result.reason if dir_result is not None else None
        direction_focused = None
        if dir_result is not None and dir_result.focused and dir_result.top1:
            direction_focused = dir_result.top1[0]
        # 模糊/低分：呈现层降级为 Entity 层 summary（设计行为，非故障）
        summary_fallback = bool(
            dir_result is not None
            and dir_result.reason in ("ambiguous", "low_score"))

        # Sleep 过滤（momo 10-04 裁决）：superseded/outdated 不参与自动召回。
        # 两路命中合并前统一过滤；被排除的不进前缀块，计数照常上报。
        # 钉住的同样排除（2026-10-17 裁决：简报必带常驻在场，召回再浮
        # 就是复读）；search 主动检索不受影响。
        from ember.memory_lifecycle import filter_recall, Lifecycle
        lifecycle_excluded = filter_recall(self, {}, session, context_start_id, context_message_ids)
        entity_hits = [h for h in entity_hits if Lifecycle(self.store.root,self.source_instance).eligible(h.entity_id)]
        excluded = [eid for eid in set(recalled_ctx.recalled_events)
                    | set(entity_event_ids)
                    if not self.sleep_state.recall_allowed(eid)
                    or self.pin_store.is_pinned(eid) or eid in lifecycle_excluded]
        if excluded:
            recalled_ctx.recalled_events = [
                eid for eid in recalled_ctx.recalled_events
                if eid not in excluded]
            entity_event_ids = [
                eid for eid in entity_event_ids if eid not in excluded]

        # 窗口过滤（2026-10-14 去原文化）：来源全部在当前窗口内的候选
        # 滤掉——原文就在现场，召回只负责窗口外/被截断前的事。
        start_turn = None
        if context_message_ids is None and context_start_id is not None:
            start_turn = self.turn_ledger.turn_at_or_after(
                session.session_id, context_start_id)
        window_excluded: list[str] = []
        if start_turn is not None:
            for eid in set(recalled_ctx.recalled_events) | set(entity_event_ids):
                spans = self.turn_spans.span_of(eid)
                if not spans:
                    continue  # 无真实出处坐标 → 保守保留
                if min(s for s, _ in spans) >= start_turn:
                    window_excluded.append(eid)
            if window_excluded:
                recalled_ctx.recalled_events = [
                    eid for eid in recalled_ctx.recalled_events
                    if eid not in window_excluded]
                entity_event_ids = [
                    eid for eid in entity_event_ids
                    if eid not in window_excluded]

        # 双路合流：Entity 区域 Event 并入（去重），来源标注留待 Presentation 扩展
        for eid in entity_event_ids:
            if eid not in recalled_ctx.recalled_events:
                recalled_ctx.recalled_events.append(eid)
                ev = self.store.get_event(eid)
                if ev.raw_ref not in recalled_ctx.source_refs:
                    recalled_ctx.source_refs.append(ev.raw_ref)

        # Relation 展开 v1（10-05 裁决）：被过滤事件的修正者顶上。
        # 在合流之后执行——修正者已在召回结果中时不算"带入"。
        # gate = 边 active + 本轮 src 被召回意图触发；confidence 不参与 gate。
        resolved_brought: list[tuple[str, str]] = []  # (修正者id, 被修正者id)
        for excluded_id in excluded:
            for rel in self.relation_store.resolvers_of(excluded_id):
                dst = self._resolve_event_id(rel.dst_event)
                if dst is None:
                    continue  # 修正者 id 无法解析（数据问题），跳过不炸
                if dst in excluded or dst in lifecycle_excluded or dst in recalled_ctx.recalled_events:
                    continue
                resolved_brought.append((dst, excluded_id))
                recalled_ctx.recalled_events.append(dst)
                ev = self.store.get_event(dst)
                if ev.raw_ref not in recalled_ctx.source_refs:
                    recalled_ctx.source_refs.append(ev.raw_ref)

        # ---- 多源共证与名额（2026-10-15 设计夜定稿）----
        # 主席=双源共证/修正者顶上/新生豁免（限 2）；单源落弱信号池
        # （席位富余才补，浮出标注）；强落选驱动注入尾部检索提示
        selected_units, gate_stats = select_recall_units(
            recalled_ctx.recalled_events, recalled_ctx.recall_trace.anchors,
            entity_hits, resolved_brought, self.store)
        recalled_ctx.recalled_events = [
            eid for u in selected_units for eid in u["event_ids"]]

        # ---- 在场去重（⑧）：本会话 2h 内已全量注入过的 → 轻量续注 ----
        for u in selected_units:
            last = max((self.recall_ledger.last_injected.get(
                            (session.session_id, e), "")
                        for e in u["event_ids"]), default="")
            if last:
                try:
                    age_h = (datetime.now(timezone.utc).astimezone()
                             - datetime.fromisoformat(last).astimezone()
                             ).total_seconds() / 3600.0
                    if age_h < 2.0:
                        u["light"] = True
                except ValueError:
                    pass

        units_for_prefix = selected_units

        # 召回记账（10-08 定稿 §3.2）：三段通路各记一行，供 Sleep 候选组装
        # 与旧 Cue 收紧消费。名额截断后记账——记的是真正注入前缀的 event。
        self._ledger_record(session, turn, recalled_ctx, entity_hits,
                            entity_event_ids, dir_result, excluded,
                            resolved_brought,
                            collector=deferred_entries)

        prefix = ""
        presentations: list[dict] = []
        if recalled_ctx.recalled_events:
            prefix = self._build_recall_prefix(
                recalled_ctx, entity_hits=entity_hits,
                resolved_brought=resolved_brought,
                summary_fallback=summary_fallback,
                direction_focused=direction_focused,
                units=units_for_prefix,
                strong_dropped=gate_stats["strong_dropped"],
                presentations=presentations)

        return {
            "prefix": prefix,
            "presentations": presentations,
            "recall_refs": {"events": self._presented_event_refs(presentations)},
            "recalled_count": len(recalled_ctx.recalled_events),
            "entity_recalled": len(entity_hits),
            "entity_names": [h.name for h in entity_hits],
            "filtered_by_sleep": len(excluded),
            "filtered_by_window": len(window_excluded),
            "dropped_by_seats": gate_stats["dropped_by_seats"],
            "weak_pooled": gate_stats["weak_pooled"],
            "strong_dropped": gate_stats["strong_dropped"],
            "observe_extra": None,
            "resolved_brought": [dst for dst, _ in resolved_brought],
            "direction_focused": direction_focused,
            "direction_reason": direction_reason,
            "summary_fallback": summary_fallback,
            "semantic_matched": semantic_matched,
            "semantic_reason": direct_recaller.last_semantic_reason,
        }


    def _ledger_record(self, session: Session, turn: int,
                       ctx: PresentationContext, entity_hits: list,
                       entity_event_ids: list[str], dir_result,
                       excluded: list[str],
                       resolved_brought: list[tuple[str, str]],
                       collector: list[dict] | None = None) -> None:
        """召回记账：三段通路各记一行（route=cue/entity/relation）。

        记账对象 = Sleep 过滤后真正带出的 event；relation 行的 via 列
        被顶上的被修正者。记账失败不阻断对话（旁路只观察）。
        """
        sid = session.session_id
        def record(*args, **kwargs):
            if collector is None:
                return self.recall_ledger.record(*args, **kwargs)
            entry = self.recall_ledger.make_entry(*args, **kwargs)
            collector.append(entry)
            return entry
        try:
            # cue 通路：按 event 聚合其命中的 distinct cues
            cue_hits = [h for h in ctx.recall_trace.anchors
                        if h.event_id in ctx.recalled_events]
            if cue_hits:
                by_event: dict[str, list[str]] = {}
                for h in cue_hits:
                    by_event.setdefault(h.event_id, [])
                    if h.cue not in by_event[h.event_id]:
                        by_event[h.event_id].append(h.cue)
                for eid, cues in by_event.items():
                    record(
                        sid, turn, route="cue", event_ids=[eid],
                        cue=cues[0] if len(cues) == 1 else None)
                # 多 cue 共命中同一 event 时合并记一行（cue 列 = 全部命中 cue）
                multi = {eid: cs for eid, cs in by_event.items() if len(cs) > 1}
                for eid, cues in multi.items():
                    record(
                        sid, turn, route="cue", event_ids=[eid], cue=";".join(cues))
            # entity 通路：每个命中实体记一行（含聚焦方向）
            for h in entity_hits:
                evs = [eid for eid in entity_event_ids
                       if eid in ctx.recalled_events]
                record(
                    sid, turn, route="entity", event_ids=evs,
                    entity_id=h.entity_id,
                    direction=(dir_result.top1[0]
                               if dir_result is not None and dir_result.focused
                               and dir_result.top1 else None))
            # relation 展开：修正者顶上
            for dst, src in resolved_brought:
                record(
                    sid, turn, route="relation", event_ids=[dst], via=src)
        except Exception:
            if collector is not None:
                raise  # A durable snapshot cannot commit a partial effect plan.
            pass  # 旧形态的统计仍是旁路观察层。


    def recall(self, query: str) -> dict[str, Any]:
        """lexical + semantic direct recall + Entity/Direction + presentation（观察用）。"""
        direct = DirectRecall(self.store)
        ctx = direct.recall(query)
        entity_recaller = EntityRecall(
            self.store, self.entity_store,
            direction_matcher=self.direction_matcher)
        entity_hits, entity_event_ids = entity_recaller.recall(query)
        dir_result = entity_recaller.last_direction_result
        summary_fallback = bool(
            dir_result is not None
            and dir_result.reason in ("ambiguous", "low_score"))
        # Sleep 过滤（10-04 裁决）：superseded/outdated 不参与自动召回
        excluded = [eid for eid in set(ctx.recalled_events)
                    | set(entity_event_ids)
                    if not self.sleep_state.recall_allowed(eid)]
        if excluded:
            ctx.recalled_events = [eid for eid in ctx.recalled_events
                                   if eid not in excluded]
            entity_event_ids = [eid for eid in entity_event_ids
                                if eid not in excluded]
        # 双路合流：Entity 区域并入（去重）
        for eid in entity_event_ids:
            if eid not in ctx.recalled_events:
                ctx.recalled_events.append(eid)
                ev = self.store.get_event(eid)
                if ev.raw_ref not in ctx.source_refs:
                    ctx.source_refs.append(ev.raw_ref)
        # Relation 展开 v1（10-05 裁决）：合流之后执行，只补真正缺失的修正者
        resolved_brought: list[tuple[str, str]] = []
        for excluded_id in excluded:
            for rel in self.relation_store.resolvers_of(excluded_id):
                dst = self._resolve_event_id(rel.dst_event)
                if dst is None:
                    continue  # 修正者 id 无法解析（数据问题），跳过不炸
                if dst in excluded or dst in ctx.recalled_events:
                    continue
                resolved_brought.append((dst, excluded_id))
                ctx.recalled_events.append(dst)
                ev = self.store.get_event(dst)
                if ev.raw_ref not in ctx.source_refs:
                    ctx.source_refs.append(ev.raw_ref)
        pres = direct.build_presentation(ctx)
        pres["entity_hits"] = [
            {"entity_id": h.entity_id, "name": h.name, "matched_via": h.matched_via}
            for h in entity_hits
        ]
        pres["direction"] = dir_result.to_dict() if dir_result is not None else None
        pres["summary_fallback"] = summary_fallback
        pres["filtered_by_sleep"] = {
            "count": len(excluded),
            "event_ids": sorted(excluded),
        }
        pres["resolved_brought"] = [
            {"resolver": dst, "superseded": src}
            for dst, src in resolved_brought
        ]
        return pres


    run_recall_pipeline = legacy_event_recall_pipeline

