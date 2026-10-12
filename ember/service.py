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

2026-10-12 拍板（MCP + hook 旁路化）：Ember 从对话主路径撤下，改以
「MCP 工具 + 轮前/轮后钩子」接入（见 hook_turn_start / hook_turn_end /
sidecar_* 一族，入口 sidecar.py）。chat_completion 代理路径保留至前端
切换完成（B1）后退役；召回流水线与写入主路已抽为共用方法，两条入口
一条实现，不养双轨。
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
from .transcript import TranscriptStore
from .dream_ledger import DreamLedger
from .dream_source import DreamSource
from .writer import (
    TOOL_NAME,
    READ_SOURCE_TOOL_NAME,
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
    semantic_matched: int = 0              # 本轮 Cue 语义段命中的 route 数（§2.4 语义扩展）
    semantic_reason: str | None = None     # ok / no_vectors / embed_failed / not_requested
    dropped_quotes: list[dict[str, str]] = field(default_factory=list)  # r4：逐字核对被丢弃的原话

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
        self.dropped_quotes = other.dropped_quotes or self.dropped_quotes
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
            "semantic_matched": self.semantic_matched,
            "semantic_reason": self.semantic_reason,
            "dropped_quotes": self.dropped_quotes,
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
        # 会话原文存档（r4 第 1 项）：每轮成功后落盘 user/assistant 原文；
        # 重启恢复计数与 read_memory_source 的数据来源
        self.transcripts = TranscriptStore(getattr(store, "root", ""))
        # heart 梦通道 v3（2026-10-10 契约定稿）：选材器 + 夜账本，均旁路。
        # DreamSource 需要 source_session 属性——Event 上动态补（见下）
        self.dream_ledger = DreamLedger(getattr(store, "root", ""))
        self.dream_source = DreamSource(
            self.store, self.turn_spans, self.turn_ledger,
            self.dream_ledger, self.relation_store)
        # 旁路形态（2026-10-12）：进行中的一轮（轮前钩子记下，MCP 写入
        # 工具自动关联出处；轮后钩子收掉）。单活跃轮——单用户部署够用。
        self._live_turn: dict[str, Any] | None = None

    # ---------- session ----------

    #: heart-chat 前端不传 session_id（OpenAI 兼容请求没有这个字段）。
    #: 2026-10-11 透明工具拍板后，session 仅作旁路账本（turn 计数/原文
    #: 存档/召回引用），不再是喂给模型的上下文来源——上下文一律以前端
    #: 发来的 messages 为准。独立会话隔离等前端支持 session 字段后再启用。
    DEFAULT_SESSION_ID = "heart-chat-main"

    def get_or_create_session(self, session_id: str | None) -> Session:
        sid = session_id or self.DEFAULT_SESSION_ID
        if sid not in self.sessions:
            sess = Session(sid)
            # 重启恢复计数（r4 第 2 项）：该 session 有存档 → 计数接续最大
            # turn，避免与重启前轮次撞号（记账/turn_span 串号）。
            # 只恢复计数，不恢复 messages 历史——上下文恢复由 momo 定为
            # 独立项（涉及 wire 层一致性/缓存/前端，另行设计）。
            saved_max = self.transcripts.max_turn(sid)
            if saved_max > 0:
                sess.turn_counter = saved_max
            self.sessions[sid] = sess
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

        # 本地 recall：全流水线抽为 run_recall_pipeline（与轮前钩子共用，
        # 2026-10-12 旁路化——两条入口一条召回，避免双轨漂移）
        memory = MemoryWriteStatus()
        memory.model = model
        rec = self.run_recall_pipeline(session, turn, user_text)
        memory.semantic_matched = rec["semantic_matched"]
        memory.semantic_reason = rec["semantic_reason"]
        memory.direction_focused = rec["direction_focused"]
        memory.direction_reason = rec["direction_reason"]
        memory.summary_fallback = rec["summary_fallback"]
        memory.entity_recalled = rec["entity_recalled"]
        memory.entity_names = rec["entity_names"]
        memory.filtered_by_sleep = rec["filtered_by_sleep"]
        memory.resolved_brought = rec["resolved_brought"]
        recall_prefix = rec["prefix"]
        if recall_prefix:
            memory.recalled_count = rec["recalled_count"]

        session.messages.append({"role": "user", "content": user_text, "turn": turn})

        # wire 层（2026-10-11 透明工具拍板）：以前端发来的消息为准。
        # 人设/历史/heart 注入属于已发生的上下文——Ember 原样透传，
        # 不增不删不改；只做三件事：recall 前缀挂本轮用户消息、递记忆
        # 工具、旁路记账。session.messages 降级为账本（供原文存档与
        # 召回引用），不再作为喂给模型的上下文来源。
        # 前端不发 messages 的独立 UI 形态：回落为单条本轮用户消息。
        wire = [_strip_internal(dict(m)) for m in incoming] or \
            [{"role": "user", "content": user_text}]
        if recall_prefix:
            for _m in reversed(wire):
                if _m.get("role") != "user":
                    continue
                _c = _m.get("content")
                if isinstance(_c, str):
                    _m["content"] = recall_prefix + _c
                elif isinstance(_c, list):
                    for _p in _c:
                        if isinstance(_p, dict) and _p.get("type") == "text":
                            _p["text"] = recall_prefix + (_p.get("text") or "")
                            break
                break
        wire = self._with_embedded_prompt(wire)

        try:
            assistant_msg = self.client.chat_completion(
                model=model,
                messages=list(wire),  # 传副本：工具循环会继续追加 wire，别污染调用记录
                tools=build_tools_spec(),
                tool_choice="auto",
                temperature=payload.get("temperature"),
            )
        except LLMError as ex:
            session.messages.pop()  # chat 失败则本轮不计入历史
            raise

        # 模型决定写记忆 / 翻原文：处理 tool call（可能多轮，上限 2 防失控）
        followup_msg = assistant_msg
        for _ in range(2):
            tool_calls = followup_msg.get("tool_calls") or []
            if not tool_calls:
                break
            history_mark = len(session.messages)
            if any((c.get("function") or {}).get("name") == READ_SOURCE_TOOL_NAME
                   for c in tool_calls):
                write_result = self._handle_read_source_call(session, tool_calls)
            else:
                write_result = self._handle_memory_tool_call(session, turn, tool_calls)
            memory.merge(write_result["status"])
            if not write_result.get("handled"):
                break
            # 工具往返消息（assistant tool_calls + tool result）同步追加到
            # wire 尾部，让模型接着说完（账本与 wire 各自追加，互不复读）
            for m in session.messages[history_mark:]:
                wire.append(_strip_internal(dict(m)))
            followup_msg = self.client.chat_completion(
                model=model,
                messages=self._with_embedded_prompt(list(wire)),
                tools=build_tools_spec(),
                tool_choice="auto",
                temperature=payload.get("temperature"),
            )

        content = followup_msg.get("content")
        if not isinstance(content, str) or not content.strip():
            content = "（空回复）"
        session.messages.append({"role": "assistant", "content": content, "turn": turn})

        # 原文存档（r4 第 1 项）：本轮成功结束，用户原话（不带召回前缀）
        # + 最终回复各存一行；chat 失败走上面 except 分支不会到这里
        try:
            self.transcripts.append_turn(session.session_id, turn,
                                         user_text, content)
        except OSError:
            pass  # 存档是旁路，落盘失败不阻断对话返回

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

    # ---------- 召回流水线（chat 与轮前钩子共用，2026-10-12 抽取） ----------

    def run_recall_pipeline(self, session: Session, turn: int,
                            user_text: str) -> dict[str, Any]:
        """本地召回全流水线：Cue/Entity 双路 → Sleep 过滤 → Relation 展开
        → 召回记账 → 前缀块构建。

        两条入口（chat_completion / hook_turn_start）共用同一实现。
        记账失败不阻断（旁路只观察）。返回：
        {prefix, recalled_count, entity_recalled, entity_names,
         filtered_by_sleep, resolved_brought, direction_focused,
         direction_reason, summary_fallback, semantic_matched,
         semantic_reason}
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
        excluded = [eid for eid in set(recalled_ctx.recalled_events)
                    | set(entity_event_ids)
                    if not self.sleep_state.recall_allowed(eid)]
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

        # 召回记账（10-08 定稿 §3.2）：三段通路各记一行，供 Sleep 候选组装
        # 与旧 Cue 收紧消费。合流后记账——记的是过滤后真正带出的 event。
        self._ledger_record(session, turn, recalled_ctx, entity_hits,
                            entity_event_ids, dir_result, excluded,
                            resolved_brought)

        prefix = ""
        if recalled_ctx.recalled_events:
            prefix = self._build_recall_prefix(
                recalled_ctx, entity_hits=entity_hits,
                resolved_brought=resolved_brought,
                summary_fallback=summary_fallback,
                direction_focused=direction_focused)

        return {
            "prefix": prefix,
            "recalled_count": len(recalled_ctx.recalled_events),
            "entity_recalled": len(entity_hits),
            "entity_names": [h.name for h in entity_hits],
            "filtered_by_sleep": len(excluded),
            "resolved_brought": [dst for dst, _ in resolved_brought],
            "direction_focused": direction_focused,
            "direction_reason": direction_reason,
            "summary_fallback": summary_fallback,
            "semantic_matched": semantic_matched,
            "semantic_reason": direct_recaller.last_semantic_reason,
        }

    # ---------- 记忆写入（对话流内，模型自主触发） ----------

    def write_validated_event(self, session: Session, turn: int,
                              validated: dict[str, Any]) -> dict[str, Any]:
        """validated 参数 → Event 落库全流程（quotes 核对 / turn_span / Routes）。

        代理工具循环与 MCP 旁路工具（2026-10-12 拍板）共用的写入主路。
        异常由调用方处理（代理记历史，MCP 回错误回执）；返回回执 dict：
        {status, event_id, summary, raw_ref, route_count, recall_cues
         [, dropped_quotes]}。
        """
        event = self._build_event(session, turn, validated["event"])
        self.store.write_event(event)

        # key_quotes 逐字核对（r4 第 3.2 项，确定性代码侧）：
        # 每条 text 在本会话原文（session.messages 中对应 role 的
        # content；用户侧用原话，不含召回前缀——历史里存的就是原话）
        # 里逐字查找；对不上的条目丢弃（不拒绝整个 Event），
        # 丢弃清单写进 tool result 让模型知道哪句没对上
        dropped = self._verify_key_quotes(event, session)

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

        result: dict[str, Any] = {
            "status": "created",
            "event_id": event.event_id,
            "summary": event.summary,
            "raw_ref": event.raw_ref,
            "route_count": len(routes),
            "recall_cues": [r["cue"] for r in routes],
        }
        if dropped:
            result["dropped_quotes"] = dropped
        return result

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
            result = self.write_validated_event(session, turn, validated)
            self._append_tool_record(session, tool_calls, result)
            status.tool_result_in_context = True
            status.status = "created"
            status.event_id = result["event_id"]
            status.summary = result["summary"]
            status.route_count = result["route_count"]
            status.recall_cues = result["recall_cues"]
            status.raw_ref = result["raw_ref"]
            status.dropped_quotes = result.get("dropped_quotes") or []
            return {"handled": True, "status": status}
        except (LLMError, ValueError, KeyError, OSError) as ex:
            # 失败也如实进上下文：模型知道自己这次没写成
            self._append_tool_record(session, tool_calls,
                                     {"status": "failed", "error": str(ex)[:300]})
            status.tool_result_in_context = True
            status.status = "failed"
            status.error = str(ex)[:300]
            return {"handled": True, "status": status}

    # ---------- 翻原文工具（r4 第 4 项，只读） ----------

    READ_SOURCE_MAX_CHARS = 4000   # 单次返回总长上限（baseline）

    def read_source_event(self, event_id: str) -> dict[str, Any]:
        """翻原文单事件实现（只读；代理工具循环与 MCP 旁路工具共用）。

        取原文 = Event 的 source_ref 解析 session + turn_spans 精确区间
        （无则降级 turn_refs 锚点），从 TranscriptStore 取这些轮的
        user/assistant 原文。无存档（旧数据）→ not_archived，不报错不编造。
        superseded 事件也可读——这是显式翻看，不是自动召回。
        """
        import re
        eid = (event_id or "").strip()
        resolved = self._resolve_event_id(eid) if eid else None
        if resolved is None:
            return {"status": "not_found", "event_id": eid,
                    "error": "找不到该 event_id"}
        event = self.store.get_event(resolved)
        sess = re.match(r"conversation/session-([^/]+)",
                        event.source_ref or "")
        spans = self.turn_spans.span_of_event(event)
        if sess is None or not spans:
            return {"status": "not_archived", "event_id": resolved,
                    "reason": "该事件没有可定位的原文存档"
                              "（写入早于存档功能或出处未回填）"}
        sid = sess.group(1)
        turns = [t for s, e in spans for t in range(s, e + 1)]
        rows = self.transcripts.get(sid, turns)
        if not rows:
            return {"status": "not_archived", "event_id": resolved,
                    "reason": "该事件的原文轮次没有存档"}
        transcript = "\n".join(
            f"[turn {r['turn']}] {r['role']}: {r['content']}" for r in rows)
        truncated = False
        if len(transcript) > self.READ_SOURCE_MAX_CHARS:
            transcript = transcript[:self.READ_SOURCE_MAX_CHARS]
            truncated = True
        return {
            "status": "ok",
            "event_id": resolved,
            "turns": turns,
            "transcript": transcript,
            "truncated": truncated,
        }

    def _handle_read_source_call(self, session: Session,
                                 tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
        """执行模型发出的 read_memory_source 调用（单事件实现见
        read_source_event，代理/MCP 共用）。只读：不写任何认知结构，
        不产生 MemoryWriteStatus 记忆事件。"""
        from .writer import _loads_lenient
        results: list[dict[str, Any]] = []
        for call in tool_calls:
            fn = (call or {}).get("function") or {}
            if fn.get("name") != READ_SOURCE_TOOL_NAME:
                results.append({"status": "ignored", "reason": "unknown tool"})
                continue
            try:
                args = _loads_lenient(fn.get("arguments") or "{}") \
                    if isinstance(fn.get("arguments"), str) else (fn.get("arguments") or {})
            except ValueError as ex:
                results.append({"status": "failed", "error": f"arguments 解析失败: {ex}"})
                continue
            results.append(self.read_source_event((args or {}).get("event_id", "")))
        self._append_tool_record(session, tool_calls, results[0] if results
                                 else {"status": "ignored"})
        status = MemoryWriteStatus()
        status.tool_called = READ_SOURCE_TOOL_NAME
        status.tool_result_in_context = True
        return {"handled": True, "status": status}

    def _with_embedded_prompt(self, wire: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把写作指令注入 system 末尾（不改用户消息；无 system 则插入一条）。

        2026-10-11 无感化拍板：EMBEDDED_WRITER_PROMPT 已退役为空串，
        写作规则全部内嵌工具描述（writer.build_tools_spec）。常量为空时
        跳过注入——TA 的 system 恢复零污染；恢复旧形态填回常量即可。
        只注入 wire 层，session.messages 不存——避免历史膨胀与重复。
        """
        out = [dict(m) for m in wire]
        if not EMBEDDED_WRITER_PROMPT.strip():
            return out
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
        lines = ["[记忆浮现]"]

        if summary_fallback:
            # Entity 层 summary 回退：只给概览，不做全文注入
            lines.append(f"你提到了{'、'.join('〈' + n + '〉' for n in entity_names)}，"
                         "这里有一段与此相关的过往经历，但暂时说不准你想从哪个角度了解：")
            # 实体名片（基本属性描述，§2.1 Entity.summary）：
            # 有就插一行帮对话模型定位"这是关于什么的"；没生成过（空）就跳过
            for h in (entity_hits or []):
                try:
                    ent = self.entity_store.get(h.entity_id)
                except KeyError:
                    continue
                if (ent.summary or "").strip():
                    lines.append(f"关于〈{h.name}〉：{ent.summary.strip()}")
            for eid in ctx.recalled_events:
                event = self.store.get_event(eid)
                lines.append(f"- {event.summary}（{eid}）")
            lines.append("如果对方想深入某一条，可以再具体问问——那时再展开原文，"
                         "或调用 read_memory_source(\"<event_id>\") 翻看。")
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
                quotes_line = self._format_key_quotes(event)
                if quotes_line:
                    lines.append(f"  {quotes_line}")
                continue
            if entity_names and not hits:
                strength = "实体区域"
            elif len(distinct_cues) >= 2:
                strength = "多个线索同时指向"
            else:
                strength = "隐约联想"
            why = "、".join(f"「{c}」" for c in distinct_cues)
            lines.append(f"- （{strength}）{core}")
            quotes_line = self._format_key_quotes(event)
            if quotes_line:
                lines.append(f"  {quotes_line}")
            if why:
                lines.append(f"  触点：{why}；需要原话时可调用 "
                             f"read_memory_source(\"{eid}\")")
            else:
                lines.append(f"  需要原话时可调用 read_memory_source(\"{eid}\")")
        lines.append("[记忆结束——自然地运用它们，不要机械复述本结构]")
        return "\n".join(lines) + "\n\n"

    @staticmethod
    def _format_key_quotes(event: Event) -> str:
        """原话行（r4 第 3.4 项）：speaker=user 标"她"，ai 标"我"；无原话返回空串。"""
        if not (getattr(event, "key_quotes", None) or []):
            return ""
        parts = []
        for q in event.key_quotes:
            who = "她" if q.get("speaker") == "user" else "我"
            parts.append(f"「{q.get('text', '')}」（{who}）")
        return "原话：" + "".join(parts)

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
            key_quotes=w_event.get("key_quotes") or [],
            provenance_refs=["memory-writer", f"chat:{session.session_id}"],
        )

    @staticmethod
    def _verify_key_quotes(event: Event, session: Session) -> list[dict[str, str]]:
        """key_quotes 逐字核对（r4 第 3.2 项，确定性，代码侧）。

        每条 text 逐字查找本会话原文（session.messages 中对应 role 的
        content；用户侧消息历史里存的就是原话，不含召回前缀）。做空白
        归一容错（与 locate_turn 同款策略）。对不上的条目从 event 上
        丢弃并返回（调用方写进 tool result）；原话的价值在于是真话，
        不能让改写或编造的句子混进去。
        """
        import re

        def _norm(s: str) -> str:
            return re.sub(r"\s+", "", s or "")

        corpus_by_role: dict[str, list[str]] = {"user": [], "assistant": []}
        for m in session.messages:
            role = m.get("role")
            if role in corpus_by_role and isinstance(m.get("content"), str):
                corpus_by_role[role].append(m["content"])

        def _find_anywhere(text: str, speaker: str) -> bool:
            t, tn = text, _norm(text)
            # 精确：任一对应 role 的消息原文包含
            for c in corpus_by_role.get(speaker, []):
                if t and t in c:
                    return True
            # 空白归一容错
            if len(tn) >= 4:
                for c in corpus_by_role.get(speaker, []):
                    if tn in _norm(c):
                        return True
            return False

        kept: list[dict[str, str]] = []
        dropped: list[dict[str, str]] = []
        for q in (event.key_quotes or []):
            speaker = "assistant" if q.get("speaker") == "ai" else "user"
            if _find_anywhere(q.get("text", ""), speaker):
                kept.append(q)
            else:
                dropped.append(q)
        event.key_quotes = kept
        return dropped

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

    # ---------- heart 联动 ----------

    def dream_material_preview(self) -> dict[str, Any]:
        """选一夜梦材料但不推送（观察/联调用）：选材结果 + 跳过明细。"""
        return self.dream_source.select()

    def push_dream_night(self, pusher=None) -> dict[str, Any]:
        """一夜的完整编排（heart v3，2026-10-10 契约）：

        选材（DreamSource）→ 一次性批量推送（DreamPusher，重试≤3）
        → 收 dream_id 即收工 → 夜账本留痕。

        无梦之夜照推空数组（heart 返回 dream_id=null，健康状态）。
        heart 拒收/不可达 → DreamPushError 上抛（统一诊断出口在调用方），
        账本记 status=failed（材料没进殿堂，旧事仍可再挑）。
        pusher 参数供测试注入 fake；生产从 .env 配置构造。
        """
        if pusher is None:
            from .dream_push import DreamPusher
            pusher = DreamPusher()
        sel = self.dream_source.select()
        try:
            result = pusher.push_night(sel["events"])
        except Exception as ex:
            self.dream_ledger.record_night(
                event_ids=sel["pushed_ids"], dream_id=None,
                skipped=sel["skipped"], status="failed")
            raise
        self.dream_ledger.record_night(
            event_ids=sel["pushed_ids"], dream_id=result.get("dream_id"),
            skipped=sel["skipped"], status="ok")
        return {
            "ok": True,
            "dream_id": result.get("dream_id"),
            "pushed_count": len(sel["events"]),
            "pushed_ids": sel["pushed_ids"],
            "associated_ids": sel["associated_ids"],
            "random_id": sel["random_id"],
            "skipped": sel["skipped"],
        }

    def dream_ledger_status(self) -> dict[str, Any]:
        """夜账本观察（夜数/入梦事件数），排障与回溯用。"""
        return self.dream_ledger.to_dict()

    def turn_ledger_status(self) -> dict[str, Any]:
        """映射账本观察（含发号连续性缺口——重新生成/重发的排查料）。"""
        return self.turn_ledger.to_dict()

    def turn_span_status(self) -> dict[str, Any]:
        return self.turn_spans.to_dict()

    def recall_ledger_status(self) -> dict[str, Any]:
        """召回记账观察（top cues / top events / 行数），排障用。"""
        return self.recall_ledger.to_dict()

    # ---------- 旁路服务（2026-10-12 拍板：MCP 工具 + 轮前/轮后钩子） ----------
    #
    # 接入形态（入口 sidecar.py，与 proxy.py 并存至前端切换完成）：
    #   轮前钩子 → 前端自行注入召回文本（Ember 不再改写任何请求）
    #   MCP 工具 → 模型经前端工具循环调用，只提供「写什么」；
    #              出处（会话/轮次/heart 钉子）由轮前钩子记下的
    #              进行中一轮自动关联——模型无需知道任何内部编号。
    #   轮后钩子 → 原文存档 + 收轮。
    # 设计约束：无感工具——上下文永远以前端为准；失败如实回执不编造。

    def hook_turn_start(self, session_id: str | None, message: str,
                        heart_turn_id: str | None = None) -> dict[str, Any]:
        """轮前钩子：开新一轮 + 本地召回，返回注入文本。

        返回 {injected, injected_text, session_id, turn, memory}。
        injected_text 为前端本轮自行注入的 [记忆浮现] 前缀块（无命中
        为 null）。同时记录进行中的一轮供 MCP 写入工具关联出处；
        同会话连续两次 turn_start（重新生成/重发）覆盖旧上下文。
        """
        session = self.get_or_create_session(session_id)
        turn = session.next_turn()
        self.turn_ledger.record(session.session_id, turn, heart_turn_id)
        rec = self.run_recall_pipeline(session, turn, message)
        session.messages.append({"role": "user", "content": message, "turn": turn})
        self._live_turn = {"session": session, "turn": turn,
                           "user_text": message}
        stats = {k: rec[k] for k in (
            "recalled_count", "entity_recalled", "entity_names",
            "filtered_by_sleep", "resolved_brought", "direction_focused",
            "direction_reason", "summary_fallback", "semantic_matched",
            "semantic_reason")}
        return {
            "injected": bool(rec["prefix"]),
            "injected_text": rec["prefix"] or None,
            "session_id": session.session_id,
            "turn": turn,
            "memory": stats,
        }

    def hook_turn_end(self, session_id: str | None,
                      assistant_reply: str,
                      turn: int | None = None) -> dict[str, Any]:
        """轮后钩子：原文存档（用户原话 + 最终回复）并收掉进行中的一轮。

        turn 校验（防串轮竞态）：用户连发两条时，前一轮的 turn_end 可能
        晚于后一轮的 turn_start 到达——对不上号的上报拒收（stale），
        不污染新一轮的出处账。缺轮后钩子的轮次不存档——与代理形态
        「chat 失败不计入历史」同语义；turn 计数允许留洞。
        """
        live = self._live_turn
        if live is None:
            return {"ok": False,
                    "reason": "no live turn（轮前钩子未调用）"}
        session = live["session"]
        if session_id and session.session_id != session_id:
            return {"ok": False,
                    "reason": f"live turn 属于会话 {session.session_id}"}
        if turn is not None and live["turn"] != turn:
            return {"ok": False,
                    "reason": f"stale turn_end（期望 turn-{live['turn']}，"
                              f"收到 turn-{turn}，疑似慢到的旧轮回报）"}
        # assistant 原话也进会话账本：key_quotes 的 ai 侧逐字核对语料
        session.messages.append({"role": "assistant",
                                 "content": assistant_reply,
                                 "turn": live["turn"]})
        try:
            self.transcripts.append_turn(session.session_id, live["turn"],
                                         live["user_text"], assistant_reply)
        except OSError:
            pass  # 存档是旁路，落盘失败不阻断收轮
        self._live_turn = None
        return {"ok": True, "session_id": session.session_id,
                "turn": live["turn"]}

    def sidecar_tool_specs(self) -> list[dict[str, Any]]:
        """MCP tools/list 工具清单——与代理形态同一份 schema 真相源。"""
        return build_tools_spec()

    def sidecar_create_memory_event(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """MCP 工具 create_memory_event 执行体：进行中的一轮 + 参数 → 落库。

        无进行中的一轮 → 如实回错（不打无出处之账）；校验失败 → failed
        回执（与代理形态同一套 validate 规则）。
        """
        live = self._live_turn
        if live is None:
            return {"status": "failed",
                    "error": "当前没有进行中的对话轮（轮前钩子未调用），"
                             "无从记录这条记忆的出处"}
        validated, err = validate_writer_arguments(arguments)
        if err:
            return {"status": "failed", "error": f"validation failed: {err}"}
        try:
            return self.write_validated_event(live["session"], live["turn"],
                                              validated)
        except (ValueError, KeyError, OSError) as ex:
            return {"status": "failed", "error": str(ex)[:300]}

    def sidecar_read_memory_source(self, event_id: str) -> dict[str, Any]:
        """MCP 工具 read_memory_source 执行体（只读，同代理形态实现）。"""
        return self.read_source_event(event_id)
