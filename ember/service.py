# -*- coding: utf-8 -*-
"""Ember v3 Phase 3 — 记忆写入自然嵌入对话流。

momo 2026-09-30 裁决（22:27，取代 Phase 2.5 的独立 writer 调用）：
1. 不再每一轮单独调一次 writer 问"有没有可写的"。
2. 模型在对话中自己觉得有需要写下的，就调 create_memory_event 写。
   写记忆是自然嵌入对话里的，不是每句都拿过去审问一遍。
3. 每轮 Ember 只在本地做 recall（毫秒级，无 LLM）：命中就把记忆
   以结构化前缀块挂在用户消息前面发过去。
4. 工具调用进模型上下文不变：模型调了工具、写入成败，下一轮自己能看到。
5. 写作指令由 Ember 注入 system 末尾；客户端自己的 system prompt 不受影响。

仍保留的边界：Chat LLM 不能直接修改 Ember Store——它只能发出 tool call，
由 Proxy/Service 校验并执行写入。
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .llm import LLMError
from .models import Event, PresentationContext, RecallRoute
from .recall import DirectRecall, EntityRecall, register_event_entity_refs
from .relation import RelationStore
from .sleep_state import SleepStateStore
from .store import EmberStore
from .entity import EntityStore
from .direction import DirectionMatcher, DirectionVectorStore
from .turn_ledger import TurnLedger
from .recall_ledger import RecallLedger
from .turn_span import TurnSpanStore, spans_from_quotes
from .writer import (
    TOOL_NAME,
    EMBEDDED_WRITER_PROMPT,
    build_tools_spec,
    validate_writer_arguments,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class MemoryWriteStatus:
    """本轮记忆活动的状态（给前端 Memory 面板）。

    Phase 3 起：记忆写入由模型在对话流中自主触发（不再有独立 writer 调用），
    另有 recall 注入信息（recalled_count）。
    """
    status: str = "skipped"          # created / skipped / failed
    writer_called: bool = False      # 本轮模型是否发出了记忆工具调用
    tool_called: str | None = None
    validation: str | None = None    # passed / <error>
    event_id: str | None = None
    summary: str | None = None
    route_count: int = 0
    must_routes: list[dict[str, str]] = field(default_factory=list)  # 已废弃，保留兼容
    may_routes: list[dict[str, str]] = field(default_factory=list)   # 已废弃，保留兼容
    recall_cues: list[str] = field(default_factory=list)             # 全部触点 cue
    raw_ref: str | None = None
    error: str | None = None
    model: str | None = None
    tool_result_in_context: bool = False
    recalled_count: int = 0          # 本轮 recall 命中并注入前缀的 Event 数
    entity_recalled: int = 0         # Entity 通路命中的 Entity 数（最小闭环）
    entity_names: list[str] = field(default_factory=list)  # 命中的 Entity 规范名
    filtered_by_sleep: int = 0       # 本轮被 Sleep 过滤掉（未注入）的命中数
    resolved_brought: list[str] = field(default_factory=list)  # 因过滤而带入的修正者 event_id
    direction_focused: str | None = None   # Direction 聚焦命中的方向 id
    direction_reason: str | None = None    # clear / ambiguous / low_score / no_vectors / embed_failed
    summary_fallback: bool = False         # 是否走了 Entity 层 summary 回退

    def merge(self, other: "MemoryWriteStatus") -> None:
        """合并一次工具调用的结果进本轮总状态。"""
        self.writer_called = other.writer_called or self.writer_called
        self.tool_called = other.tool_called or self.tool_called
        self.validation = other.validation or self.validation
        self.event_id = other.event_id or self.event_id
        self.summary = other.summary or self.summary
        self.route_count += other.route_count
        self.recall_cues += other.recall_cues
        self.raw_ref = other.raw_ref or self.raw_ref
        self.error = other.error or self.error
        self.tool_result_in_context = self.tool_result_in_context or other.tool_result_in_context
        if other.status != "skipped":
            self.status = other.status

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "writer_called": self.writer_called,
            "tool_called": self.tool_called,
            "validation": self.validation,
            "event_id": self.event_id,
            "summary": self.summary,
            "route_count": self.route_count,
            "recall_cues": self.recall_cues,
            "raw_ref": self.raw_ref,
            "error": self.error,
            "model": self.model,
            "tool_result_in_context": self.tool_result_in_context,
            "recalled_count": self.recalled_count,
            "entity_recalled": self.entity_recalled,
            "entity_names": self.entity_names,
            "filtered_by_sleep": self.filtered_by_sleep,
            "resolved_brought": self.resolved_brought,
            "direction_focused": self.direction_focused,
            "direction_reason": self.direction_reason,
            "summary_fallback": self.summary_fallback,
        }


class Session:
    """一个会话：完整 OpenAI 格式对话历史（含 tool_calls / tool 消息）+ turn 计数。

    这是唯一的上下文真相源：chat 与 memory writer 共用它；
    换模型不换 session，历史原样延续。
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.messages: list[dict[str, Any]] = []
        self.turn_counter = 0

    def next_turn(self) -> int:
        self.turn_counter += 1
        return self.turn_counter

    def raw_ref(self, turn: int) -> str:
        return f"conversation/session-{self.session_id}/turn-{turn}"

    def wire_messages(self) -> list[dict[str, Any]]:
        """转成 provider 可接受的 wire 格式：剥掉内部标记键（turn 等）。"""
        out = []
        for m in self.messages:
            w = {"role": m["role"]}
            if "content" in m and m["content"] is not None:
                w["content"] = m["content"]
            if "tool_calls" in m and m["tool_calls"]:
                w["tool_calls"] = m["tool_calls"]
            if "tool_call_id" in m:
                w["tool_call_id"] = m["tool_call_id"]
            out.append(w)
        return out


def _strip_internal(m: dict[str, Any]) -> dict[str, Any]:
    """对外（provider）发送时剥掉内部键。"""
    w = {"role": m["role"]}
    if m.get("content") is not None:
        w["content"] = m["content"]
    if m.get("tool_calls"):
        w["tool_calls"] = m["tool_calls"]
    if m.get("tool_call_id"):
        w["tool_call_id"] = m["tool_call_id"]
    return w


class EmberService:
    """Ember Proxy 的核心：单模型 chat + memory write（共用同一份 session 历史）。"""

    def __init__(
        self,
        store: EmberStore,
        client,
        model: str = "default",
        recall_context: bool = False,
    ):
        self.store = store
        self.client = client          # chat 与 memory writer 共用
        self.model = model
        # 未来开关（本阶段文档已预留）：把 Recall Context 注入本轮对话
        self.recall_context = recall_context
        self.sessions: dict[str, Session] = {}
        # Entity 层（最小闭环）：与 store 同目录加载 entities.jsonl
        self.entity_store = EntityStore(getattr(store, "root", ""))
        register_event_entity_refs(
            os.path.join(getattr(store, "root", ""), "events.jsonl"))
        # Sleep 状态旁路层：superseded/outdated 不参与自动召回（10-04 裁决）
        self.sleep_state = SleepStateStore(getattr(store, "root", ""))
        # Relation 层 v1：resolves 边（被修正者 → 修正者），
        # 被过滤事件的修正者顶上（"旧观点被什么取代了"的入口）
        self.relation_store = RelationStore(getattr(store, "root", ""))
        # Direction 匹配层（10-05 通电）：向量做判断（选方向/判模糊），
        # 不做直接召回；方向向量由 scripts/build_direction_vectors.py 离线预计算
        self.direction_matcher = DirectionMatcher(
            self.entity_store,
            DirectionVectorStore(getattr(store, "root", "")))
        # heart 联动（10-06）：turn 映射账本 + turn_span 回填层，均旁路
        self.turn_ledger = TurnLedger(getattr(store, "root", ""))
        self.turn_spans = TurnSpanStore(getattr(store, "root", ""))
        # 召回记账（10-08 定稿 §3.2）：候选组装与旧 Cue 收紧的共同前置，旁路只观察
        self.recall_ledger = RecallLedger(getattr(store, "root", ""))

    # ---------- session ----------

    def get_or_create_session(self, session_id: str | None) -> Session:
        sid = session_id or uuid.uuid4().hex[:12]
        if sid not in self.sessions:
            self.sessions[sid] = Session(sid)
        return self.sessions[sid]

    # ---------- chat ----------

    def chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        """处理 OpenAI-compatible chat request，返回兼容 response。

        主路径（momo 22:27 裁决）：
        1. 本地 recall（无 LLM）——命中则把记忆以结构化前缀块挂在本轮
           用户消息前面（仅注入 wire 层，session 历史里存用户原话）。
        2. 一次 LLM 调用：对话照常 + tools 在场，模型自己决定是否写记忆。
        3. 若模型发出 tool call：校验→写入→tool result 回传→再调一次让
           模型接着说完。没写就是一次调用，延迟不翻倍。
        """
        session = self.get_or_create_session(
            payload.get("session_id") or payload.get("user") or payload.get("ember_session"))
        incoming = payload.get("messages") or []
        user_msgs = [m for m in incoming if m.get("role") == "user"]
        user_text = user_msgs[-1].get("content", "") if user_msgs else ""
        model = payload.get("model") or self.model
        turn = session.next_turn()

        # heart 联动（10-06）：heart_meta.turn_id 管道层记账。
        # 缺失不阻断——heart 侧 forward_turn_meta 默认关，打开前照常运行。
        heart_meta = payload.get("heart_meta") or {}
        self.turn_ledger.record(session.session_id, turn,
                                heart_meta.get("turn_id"))

        # 本地 recall：只看本轮用户输入，命中才注入
        memory = MemoryWriteStatus()
        memory.model = model
        recalled_ctx = DirectRecall(self.store).recall(user_text)

        # Entity 通路（最小闭环 + Direction 聚焦，10-05 通电）：
        # 输入明确指向 Entity → 判断方向 → 聚焦该方向 / 或全区域+summary 回退
        entity_recaller = EntityRecall(
            self.store, self.entity_store,
            direction_matcher=self.direction_matcher)
        entity_hits, entity_event_ids = entity_recaller.recall(user_text)
        dir_result = entity_recaller.last_direction_result
        if dir_result is not None:
            memory.direction_reason = dir_result.reason
            if dir_result.focused and dir_result.top1:
                memory.direction_focused = dir_result.top1[0]
            # 模糊/低分：呈现层降级为 Entity 层 summary（设计行为，非故障）
            memory.summary_fallback = dir_result.reason in ("ambiguous", "low_score")
        memory.entity_recalled = len(entity_hits)
        memory.entity_names = [h.name for h in entity_hits]

        # Sleep 过滤（momo 10-04 裁决）：superseded/outdated 不参与自动召回。
        # 两路命中合并前统一过滤；被排除的不进前缀块，但计数上报给前端。
        excluded = [eid for eid in set(recalled_ctx.recalled_events)
                    | set(entity_event_ids)
                    if not self.sleep_state.recall_allowed(eid)]
        memory.filtered_by_sleep = len(excluded)
        if excluded:
            recalled_ctx.recalled_events = [
                eid for eid in recalled_ctx.recalled_events
                if eid not in excluded]
            entity_event_ids = [
                eid for eid in entity_event_ids if eid not in excluded]

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
                if dst in excluded or dst in recalled_ctx.recalled_events:
                    continue
                resolved_brought.append((dst, excluded_id))
                recalled_ctx.recalled_events.append(dst)
                ev = self.store.get_event(dst)
                if ev.raw_ref not in recalled_ctx.source_refs:
                    recalled_ctx.source_refs.append(ev.raw_ref)
        memory.resolved_brought = [dst for dst, _ in resolved_brought]

        # 召回记账（10-08 定稿 §3.2）：三段通路各记一行，供 Sleep 候选组装
        # 与旧 Cue 收紧消费。合流后记账——记的是过滤后真正带出的 event。
        self._ledger_record(session, turn, recalled_ctx, entity_hits,
                            entity_event_ids, dir_result, excluded,
                            resolved_brought)

        recall_prefix = ""
        if recalled_ctx.recalled_events:
            recall_prefix = self._build_recall_prefix(
                recalled_ctx, entity_hits=entity_hits,
                resolved_brought=resolved_brought,
                summary_fallback=memory.summary_fallback,
                direction_focused=memory.direction_focused)
            memory.recalled_count = len(recalled_ctx.recalled_events)

        session.messages.append({"role": "user", "content": user_text, "turn": turn})

        # wire 层：历史 + 本轮（用户消息带 recall 前缀）+ 注入的写作指令
        wire = [_strip_internal(m) for m in session.messages]
        if recall_prefix:
            wire[-1] = dict(wire[-1])
            wire[-1]["content"] = recall_prefix + (wire[-1].get("content") or "")
        wire = self._with_embedded_prompt(wire)

        try:
            assistant_msg = self.client.chat_completion(
                model=model,
                messages=wire,
                tools=build_tools_spec(),
                tool_choice="auto",
                temperature=payload.get("temperature"),
            )
        except LLMError as ex:
            session.messages.pop()  # chat 失败则本轮不计入历史
            raise

        # 模型决定写记忆：处理 tool call（可能多轮，上限 2 防失控）
        followup_msg = assistant_msg
        for _ in range(2):
            tool_calls = followup_msg.get("tool_calls") or []
            if not tool_calls:
                break
            write_result = self._handle_memory_tool_call(session, turn, tool_calls)
            memory.merge(write_result["status"])
            if not write_result.get("handled"):
                break
            # tool result 已入历史，让模型接着说完
            followup_msg = self.client.chat_completion(
                model=model,
                messages=self._with_embedded_prompt(
                    [_strip_internal(m) for m in session.messages]),
                tools=build_tools_spec(),
                tool_choice="auto",
                temperature=payload.get("temperature"),
            )

        content = followup_msg.get("content")
        if not isinstance(content, str) or not content.strip():
            content = "（空回复）"
        session.messages.append({"role": "assistant", "content": content, "turn": turn})

        return {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion",
            "created": int(datetime.now(timezone.utc).timestamp()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": followup_msg.get("usage") or {},
            "ember": {
                "session_id": session.session_id,
                "turn": turn,
                "model": model,
                "memory": memory.to_dict(),
                "heart_meta": {
                    "turn_id": self.turn_ledger.heart_id_of(session.session_id, turn),
                    "ledger_size": len(self.turn_ledger.mapping),
                },
            },
        }

    # ---------- 记忆写入（对话流内，模型自主触发） ----------

    def _handle_memory_tool_call(self, session: Session, turn: int,
                                 tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
        """校验并执行模型在对话中发出的 create_memory_event 调用。

        无论成败，assistant(tool_calls) + tool(result) 都追加进 session
        历史——模型下一轮知道自己调用过工具、结果如何（momo Phase 2.5 裁决）。
        返回 {handled, status(MemoryWriteStatus)}。
        """
        status = MemoryWriteStatus()
        status.model = session and self.model
        status.writer_called = True
        status.tool_called = TOOL_NAME

        raw_args = None
        for call in tool_calls:
            fn = (call or {}).get("function") or {}
            if fn.get("name") == TOOL_NAME:
                raw_args = fn.get("arguments") or "{}"
                break
        if raw_args is None:
            # 调了别的工具：如实回执，不算 memory 事件
            self._append_tool_record(session, tool_calls,
                                     {"status": "ignored", "reason": "unknown tool"})
            return {"handled": True, "status": status}

        def _fail(err: str) -> dict[str, Any]:
            self._append_tool_record(session, tool_calls,
                                     {"status": "failed", "error": err[:300]})
            status.tool_result_in_context = True
            status.status = "failed"
            status.error = err
            return {"handled": True, "status": status}

        try:
            from .writer import _loads_lenient
            args = _loads_lenient(raw_args) if isinstance(raw_args, str) else raw_args
        except ValueError as ex:
            return _fail(f"tool arguments 解析失败: {ex}")

        validated, err = validate_writer_arguments(args)
        if err:
            return _fail(f"validation failed: {err}")
        status.validation = "passed"

        try:
            event = self._build_event(session, turn,
                                      validated["event"])
            self.store.write_event(event)

            # 出处句对 → 精确 turn_span（§4.1：起尾句由 AI 随写入给出，
            # 代码侧确定性定位；定位失败/未给 → 粗锚点 turn_refs 兜底）
            quote_spans = validated.get("quote_spans") or []
            if quote_spans:
                pairs = spans_from_quotes(quote_spans, session.messages)
                if pairs:
                    self.turn_spans.fill(event.event_id, pairs,
                                         filled_by="memory-writer")

            routes = []
            for r in validated["recall_routes"]:
                route = RecallRoute(
                    route_id=f"rt_{event.event_id}_{len(routes) + 1:02d}",
                    event_id=event.event_id,
                    matcher="lexical",          # 第一阶段只落 lexical
                    cue_type=r["cue_type"],
                    cue=r["cue"],
                    status="active",
                    evidence_refs=[session.raw_ref(turn)],
                    provenance_refs=["memory-writer"],
                )
                self.store.write_route(route)
                routes.append({"route_id": route.route_id,
                               "cue_type": r["cue_type"], "cue": r["cue"]})

            result = {
                "status": "created",
                "event_id": event.event_id,
                "summary": event.summary,
                "raw_ref": event.raw_ref,
                "route_count": len(routes),
                "recall_cues": [r["cue"] for r in routes],
            }
            self._append_tool_record(session, tool_calls, result)
            status.tool_result_in_context = True
            status.status = "created"
            status.event_id = event.event_id
            status.summary = event.summary
            status.route_count = len(routes)
            status.recall_cues = [r["cue"] for r in routes]
            status.raw_ref = event.raw_ref
            return {"handled": True, "status": status}
        except (LLMError, ValueError, KeyError, OSError) as ex:
            # 失败也如实进上下文：模型知道自己这次没写成
            self._append_tool_record(session, tool_calls,
                                     {"status": "failed", "error": str(ex)[:300]})
            status.tool_result_in_context = True
            status.status = "failed"
            status.error = str(ex)[:300]
            return {"handled": True, "status": status}

    def _with_embedded_prompt(self, wire: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把写作指令注入 system 末尾（不改用户消息；无 system 则插入一条）。

        只注入 wire 层，session.messages 不存——避免历史膨胀与重复。
        """
        out = [dict(m) for m in wire]
        for i, m in enumerate(out):
            if m.get("role") == "system":
                m["content"] = (m.get("content") or "") + "\n\n" + EMBEDDED_WRITER_PROMPT
                return out
        return [{"role": "system", "content": EMBEDDED_WRITER_PROMPT}] + out

    def _build_recall_prefix(self, ctx: PresentationContext,
                             entity_hits: list | None = None,
                             resolved_brought: list[tuple[str, str]] | None = None,
                             summary_fallback: bool = False,
                             direction_focused: str | None = None) -> str:
        """结构化前缀块：被唤起的记忆挂在用户消息前面（momo 裁决格式）。

        指向强度分层（momo 2026-10-01 裁决：无 must/may，纯运行时聚合）：
        - Entity 明确指向 → 整个区域带出，标注"因提到〈实体名〉想起"
        - Direction 聚焦（10-05 通电）→ 标注从哪个方向进入
        - 多触点共命中（multi_cue >= 2）→ 强指向
        - 单触点 → 弱联想
        - Relation 带回（v1）→ 标注"旧观点已被新认知取代"
        summary_fallback（momo 10-04 裁决）：Direction 模糊/低分时不做
        全文注入，降级为 Entity 层 summary——事件摘要 + id，留主动检索入口。
        """
        entity_names = [h.name for h in (entity_hits or [])]
        resolved_map = dict(resolved_brought or [])
        lines = ["[Ember 记忆浮现]"]

        if summary_fallback:
            # Entity 层 summary 回退：只给概览，不做全文注入
            lines.append(f"你提到了{'、'.join('〈' + n + '〉' for n in entity_names)}，"
                         "这里有一段与此相关的过往经历，但暂时说不准你想从哪个角度了解：")
            for eid in ctx.recalled_events:
                event = self.store.get_event(eid)
                lines.append(f"- {event.summary}（{eid}）")
            lines.append("如果对方想深入某一条，可以再具体问问——那时再展开原文。")
            lines.append("[记忆概览结束——自然地运用，不要机械复述本结构]")
            return "\n".join(lines) + "\n\n"

        if entity_names:
            lead = (f"你提到了{'、'.join('〈' + n + '〉' for n in entity_names)}，"
                    "与此相关的过往经历整体浮现")
            if direction_focused:
                lead += f"（沿着「{direction_focused.replace('dir_ember_', '').replace('_', ' ')}」这条线）"
            lines.append(lead + "：")
        else:
            lines.append("以下是你因当前这句话想起的过往经历：")
        for eid in ctx.recalled_events:
            event = self.store.get_event(eid)
            hits = [h for h in ctx.recall_trace.anchors if h.event_id == eid]
            distinct_cues = sorted({h.cue for h in hits})
            core = event.core or event.summary
            if eid in resolved_map:
                superseded = self.store.get_event(resolved_map[eid])
                lines.append(f"- （修正承接）{core}")
                lines.append(f"  你曾有过「{superseded.summary}」的认知，"
                             "但它已被本次经历修正/取代——以上述新认知为准。")
                continue
            if entity_names and not hits:
                strength = "实体区域"
            elif len(distinct_cues) >= 2:
                strength = "多个线索同时指向"
            else:
                strength = "隐约联想"
            why = "、".join(f"「{c}」" for c in distinct_cues)
            lines.append(f"- （{strength}）{core}")
            if why:
                lines.append(f"  触点：{why}；原文在 {event.raw_ref}")
            else:
                lines.append(f"  原文在 {event.raw_ref}")
        lines.append("[记忆结束——自然地运用它们，不要机械复述本结构]")
        return "\n".join(lines) + "\n\n"

    def _append_tool_record(self, session: Session,
                            tool_calls: list[dict[str, Any]],
                            result: dict[str, Any]) -> None:
        """把 assistant(tool_calls) + tool(result) 追加进 session 历史。

        这就是"AI 知道自己调用工具了并且成功了"的机制：下一轮 chat/writer
        请求里，模型会看到自己上一轮的 tool call 与执行结果。
        """
        session.messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": tool_calls,
        })
        session.messages.append({
            "role": "tool",
            "tool_call_id": (tool_calls[0] or {}).get("id") or "call_ember",
            "content": json.dumps(result, ensure_ascii=False),
        })

    def _resolve_event_id(self, eid: str) -> str | None:
        """把 event_id 引用解析成 store 里的完整 id（容忍短前缀），失败返回 None。"""
        if eid in self.store._events:
            return eid
        short = eid.split("-")
        prefix = "-".join(short[:2]) if len(short) >= 2 else eid
        for known in self.store._events:
            if known.startswith(prefix):
                return known
        return None

    def _build_event(self, session: Session, turn: int,
                     w_event: dict[str, Any]) -> Event:
        """系统字段由 Proxy/Store 生成，LLM 提供的字段只进 content。"""
        event_id = f"EVT-{len(self.store.all_events()) + 1:03d}-{uuid.uuid4().hex[:6]}"
        return Event(
            event_id=event_id,
            version_id=f"{event_id}-v1",
            source_ref=f"conversation/session-{session.session_id}",
            raw_ref=session.raw_ref(turn),
            event_time=_now_iso(),
            ingest_time=_now_iso(),
            turn_refs=[f"turn-{turn}"],
            summary=w_event["summary"],
            participants=w_event["participants"],
            objects=w_event["objects"],
            context=[w_event["context"]],
            core=w_event["core"],
            outcome=[w_event["outcome"]],
            state=[w_event["state"]],
            affect=[w_event["affect"]],
            significance=w_event["significance"],
            # change 不由 Writer 提供，留给 Sleep 补写
            details=w_event["details"],
            provenance_refs=["memory-writer", f"chat:{session.session_id}"],
        )

    # ---------- recall（复用第一阶段） ----------

    def _ledger_record(self, session: Session, turn: int,
                       ctx: PresentationContext, entity_hits: list,
                       entity_event_ids: list[str], dir_result,
                       excluded: list[str],
                       resolved_brought: list[tuple[str, str]]) -> None:
        """召回记账：三段通路各记一行（route=cue/entity/relation）。

        记账对象 = Sleep 过滤后真正带出的 event；relation 行的 via 列
        被顶上的被修正者。记账失败不阻断对话（旁路只观察）。
        """
        sid = session.session_id
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
                    self.recall_ledger.record(
                        sid, turn, route="cue", event_ids=[eid],
                        cue=cues[0] if len(cues) == 1 else None)
                # 多 cue 共命中同一 event 时合并记一行（cue 列 = 全部命中 cue）
                multi = {eid: cs for eid, cs in by_event.items() if len(cs) > 1}
                for eid, cues in multi.items():
                    self.recall_ledger.record(
                        sid, turn, route="cue", event_ids=[eid], cue=";".join(cues))
            # entity 通路：每个命中实体记一行（含聚焦方向）
            for h in entity_hits:
                evs = [eid for eid in entity_event_ids
                       if eid in ctx.recalled_events]
                self.recall_ledger.record(
                    sid, turn, route="entity", event_ids=evs,
                    entity_id=h.entity_id,
                    direction=(dir_result.top1[0]
                               if dir_result is not None and dir_result.focused
                               and dir_result.top1 else None))
            # relation 展开：修正者顶上
            for dst, src in resolved_brought:
                self.recall_ledger.record(
                    sid, turn, route="relation", event_ids=[dst], via=src)
        except Exception:
            pass  # 记账是旁路观察层，绝不阻断主对话

    def recall(self, query: str) -> dict[str, Any]:
        """lexical direct recall + Entity/Direction + presentation（观察用）。"""
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

    def session_messages(self, session_id: str) -> list[dict[str, Any]] | None:
        """调试观察：当前 session 的完整模型上下文（含工具调用记录）。"""
        session = self.sessions.get(session_id)
        if session is None:
            return None
        return session.wire_messages()

    def view_event(self, event_id: str) -> dict[str, Any] | None:
        """前端点击 Event 查看完整内容（只读观察，不是 memory editor）。"""
        try:
            event = self.store.get_event(event_id)
        except KeyError:
            return None
        return {
            "event": event.to_dict(),
            "recall_routes": [r.to_dict() for r in self.store.routes_for_event(event_id)],
        }

    def list_events(self) -> list[dict[str, Any]]:
        return [
            {
                "event_id": e.event_id,
                "summary": e.summary,
                "ingest_time": e.ingest_time,
                "raw_ref": e.raw_ref,
                "route_count": len(self.store.routes_for_event(e.event_id)),
            }
            for e in self.store.all_events()
        ]

    # ---------- heart 联动（10-06） ----------

    def fragment_source(self, limit: int = 20,
                        event_ids: list[str] | None = None) -> list[dict[str, Any]]:
        """FragmentSource 协议的最小实现：只产出出处，感受指纹 heart 现算。

        片段格式（heart 2026-10-06 改版）：
        - text: core + summary 拼接（heart 不需要懂 Event schema）
        - turn_span: ember 侧覆盖区间（已回填用精确值，未回填降级粗锚点）
        - salience 等：不带——heart 按 turn_span 查自己的 echo_digest
        """
        events = ([self.store.get_event(eid) for eid in event_ids
                   if eid in self.store._events]
                  if event_ids else self.store.all_events())
        out = []
        for ev in events:
            spans = self.turn_spans.span_of_event(ev)
            # Fragment 协议（heart 2026-10-06）：区间列表，允许断开；
            # 未知出处为空列表
            out.append({
                "event_id": ev.event_id,
                "text": (ev.core or ev.summary),
                "summary": ev.summary,
                "turn_span": {"ember_spans": [list(p) for p in spans]},
                "source_ref": ev.source_ref,
                "raw_ref": ev.raw_ref,
            })
        return out[:limit]

    def turn_ledger_status(self) -> dict[str, Any]:
        """映射账本观察（含发号连续性缺口——重新生成/重发的排查料）。"""
        return self.turn_ledger.to_dict()

    def turn_span_status(self) -> dict[str, Any]:
        return self.turn_spans.to_dict()

    def recall_ledger_status(self) -> dict[str, Any]:
        """召回记账观察（top cues / top events / 行数），排障用。"""
        return self.recall_ledger.to_dict()
