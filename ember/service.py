# -*- coding: utf-8 -*-
"""Ember sidecar 编排：召回、显式记忆工具、来源核验与 sleep 维护。

对话和模型工具循环由 App 持有；此模块不代理模型对话。
"""
from __future__ import annotations

import copy
import json
import os
import threading
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import Event, RecallRoute
from .recall import (register_event_entity_refs,
                     _event_entity_refs)
from .relation import RelationStore
from .sleep_state import SleepStateStore
from .store import EmberStore, EVENTS_FILE
from .entity import EntityStore
from .recall_embedding import embed_text
from .turn_ledger import TurnLedger
from .recall_ledger import RecallLedger
from .turn_span import TurnSpanStore, spans_from_quotes
from . import source_client
from .source_paths import (generation_messages, read_messages, legacy_messages,
                           current_path_corpus, SourceCorpus, QuoteSourceError)
from .pin_store import PinStore
from .source_refs import EventSourceStore, event_reference, message_seq
from .generations import GenerationStore, fingerprint, identity
from .event_mutation import SupersessionError
from .runs import RunStore, failure as run_failure
from .source_completion import SourceCompletions
from .core_semantic import (CoreVectorStore, EntitySummaryVectorStore,
                            SEARCH_HIT_FLOOR, cosine)
from .writer import (
    PIN_TOOL_NAME, UNPIN_TOOL_NAME, REVISE_TOOL_NAME,
    TOOL_NAME, PROMISE_TOOL_NAME,
    READ_SOURCE_TOOL_NAME,
    build_revise_tool_spec,
    build_pin_tool_specs,
    build_tools_spec,
    validate_writer_arguments, writer_error_result,
    SEARCH_TOOL_NAME,
    build_search_tool_spec,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Session:
    """一个会话的临时原文取材和 turn 计数。

    对话上下文归 App；此处仅作 hooks、出处核验与恢复记账。
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


def _event_entity_of(store, eid: str) -> str | None:
    """事件的实体归属（单归属铁律取第一个）。

    双源：运行时 Event 对象（dataclass 无此字段则回落）∪ jsonl 侧
    entity_refs 注册表（service 构造时 register_event_entity_refs 读入）。
    """
    try:
        ev = store.get_event(eid)
    except KeyError:
        return None
    refs = getattr(ev, "entity_refs", None) or _event_entity_refs(eid)
    return refs[0] if refs else None


def _event_ingest_ts(store, eid: str) -> float:
    try:
        return datetime.fromisoformat(store.get_event(eid).ingest_time).timestamp()
    except (KeyError, ValueError, TypeError):
        return 0.0




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
        self.source_instance = "default"
        self.client = client          # 保留构造器调用兼容；sidecar 不调用对话模型
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
        # 内容语义向量（2026-10-15 设计夜）：core 全事件 + 实体 summary——
        # search 捞出与实体卡的匹配地基；离线重建+夜批增量
        self.core_vectors = CoreVectorStore(getattr(store, "root", ""))
        self.entity_vectors = EntitySummaryVectorStore(
            getattr(store, "root", ""))
        # heart 联动（10-06）：turn 映射账本 + turn_span 回填层，均旁路
        self.turn_ledger = TurnLedger(getattr(store, "root", ""))
        self.turn_spans = TurnSpanStore(getattr(store, "root", ""))
        self.event_sources = EventSourceStore(getattr(store, "root", ""))
        # 召回记账（10-08 定稿 §3.2）：候选组装与旧 Cue 收紧的共同前置，旁路只观察
        self.recall_ledger = RecallLedger(getattr(store, "root", ""))
        # 钉住登记（2026-10-17 设计夜：briefing 必带 + 自动召回不再浮现）
        def pin_changed(eid):
            event = self.store._events.get(eid)
            if event and event.kind == 'relationship':
                from .timeline_work import request
                request(self.store.root)
        self.pin_store = PinStore(getattr(store, "root", ""), on_change=pin_changed)
        # 会话原文存档已退役（2026-10-14 去原文化）：原文只存后端消息树
        # 一份，ember 经 source_client 按 message_id 按需拉取（用完即弃）；
        # 轮次持久账 = turn_ledger（无条件记账，重启恢复计数用）
        # （旧 heart 梦通道 v3——dream_source/dream_push/夜账本——已于
        # 2026-10-15 退役：新版设计里梦归 app 的 dream_submit 工具）
        # 旁路形态（2026-10-12）：进行中的一轮（轮前钩子记下，MCP 写入
        # 工具自动关联出处；轮后钩子收掉）。单活跃轮——单用户部署够用。
        self._live_turn: dict[str, Any] | None = None
        self.generations = GenerationStore(getattr(store, "root", ""))
        self.runs = RunStore(getattr(store, "root", ""))
        self.source_completions = SourceCompletions(self.generations, self.event_sources)
        self._generation_lock = threading.RLock()
        self._briefing_lock = threading.RLock()
        self._generation_contexts: dict[str, dict] = {}
        self._volatile_tool_results: dict[tuple[str, str], dict] = {}
        from .promises import Promises
        Promises(self).sweep()
        try:
            self._reconcile_recall_effects()
        except Exception:
            pass  # Keep durable pending effects; reads/retries can reconcile later.

    # ---------- session ----------

    #: 无 session_id 的旧 hook 调用保留默认会话名；正常请求使用显式会话身份。
    DEFAULT_SESSION_ID = "heart-chat-main"

    def get_or_create_session(self, session_id: str | None) -> Session:
        sid = session_id or self.DEFAULT_SESSION_ID
        if sid not in self.sessions:
            sess = Session(sid)
            # 重启恢复计数（r4 第 2 项；2026-10-14 改源）：turn_ledger 无条件
            # 记账（transcripts 退役后账本是轮次唯一持久账），计数接续最大
            # turn，避免与重启前轮次撞号（记账/turn_span 串号）。
            # 只恢复计数，不恢复 messages 历史——上下文恢复由 momo 定为
            # 独立项（涉及 wire 层一致性/缓存/前端，另行设计）。
            saved_max = max(self.turn_ledger.max_turn(sid), self.generations.max_turn(sid))
            if saved_max > 0:
                sess.turn_counter = saved_max
            self.sessions[sid] = sess
        return self.sessions[sid]


    # ---------- 召回流水线（chat 与轮前钩子共用，2026-10-12 抽取） ----------

    def run_recall_pipeline(self, session: Session, turn: int,
                            user_text: str,
                            context_start_id: int | None = None,
                            deferred_entries: list[dict] | None = None,
                            context_message_ids: list[int] | None = None
                            ) -> dict[str, Any]:
        """Retain Event/Entity recall; raw-slice retrieval is an independent branch."""
        from .hybrid_recall import automatic_hybrid_recall
        return automatic_hybrid_recall(self, session, turn, user_text, context_start_id, deferred_entries, context_message_ids)


    # ---------- 记忆写入（对话流内，模型自主触发） ----------

    def write_validated_event(self, session: Session, turn: int,
                              validated: dict[str, Any],
                              writer_recall_id: str | None = None,
                              writer_tool_call_id: str | None = None) -> dict[str, Any]:
        """validated 参数 → Event 落库全流程（quotes 核对 / turn_span / Routes）。

        显式记忆工具共用的写入主路。
        异常由调用方转换为 MCP 错误回执；返回回执 dict：
        {status, event_id, summary, raw_ref, route_count, recall_cues
         [, dropped_quotes]}。
        """
        from .event_mutation import replacement_targets, apply_replacement
        targets=replacement_targets(self,validated)
        event = self._build_event(session, turn, validated["event"])

        # 出处句对定位与拒绝判定必须先于落盘：双盲句对（提供了 quote_spans
        # 却一对都定位不上）在写库前拒绝，不留「已入库但回执 failed」的
        # 半成品——否则模型按回执重试就会写出重复事件。
        # 2026-10-12 裁决：定位语料=持久存档∪内存（与 key_quotes 同源）；
        # 半有效句对取就近边界（单端定位 ±10 条消息、首尾同句 ±3 轮，
        # 2026-10-17 momo 改定）；双盲 → 整条写入拒绝（模型引了记录里
        # 不存在的句子，粗锚点兜底会把错误坐标写进账本，宁可报错让它
        # 逐字重摘）。
        # quote_spans 必填非空（writer.validate 封死省略路径）——粗锚点只
        # 作为旧事件的查询时降级存在，新事件一律带真实时间坐标。
        quote_spans = validated["quote_spans"]
        corpus = self._quote_corpus(session)
        span_pairs = spans_from_quotes(quote_spans, corpus)
        if not span_pairs:
            raise QuoteSourceError(getattr(corpus, "issue", None) or "quote_not_found")

        self._validate_focus(event, corpus, span_pairs)

        self.event_sources.capture(
            event.event_id, event.version_id, session.session_id, span_pairs, corpus,
            writer_recall_id=writer_recall_id, writer_tool_call_id=writer_tool_call_id,
            writer_turn=turn)
        self.store.write_event(event)


        if span_pairs:
            self.turn_spans.fill(event.event_id, span_pairs,
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
        apply_replacement(self,event,targets)
        from .promises import Promises
        Promises(self).written(event)
        from .source_sync import request_sync
        request_sync(self)
        # Derived memberships can be rebuilt after source completion.
        try:
            from .event_slices import sync_from_files
            sync_from_files(self.store.root, self.source_instance)
        except Exception:
            pass  # Derived span membership can be rebuilt independently.
        return result


    # ---------- 翻原文工具（r4 第 4 项，只读） ----------

    READ_SOURCE_MAX_CHARS = 4000   # 单次返回总长上限（baseline）

    def read_source_event(self, event_id: str, *, truncate=True, merge_sources=True) -> dict[str, Any]:
        """翻原文单事件实现（只读；MCP 与召回呈现共用）。

        Frozen provenance supplies exact message IDs; the provider checks the
        historical parent path. Missing identities, versions or provider support
        return not_archived. No current-session or neighbouring-turn fallback.
        """
        from .knowledge_tools import read_memory
        if merge_sources:
            from .memory_lifecycle import Lifecycle
            from .pin_policy import Pins
            event_id=Lifecycle(self.store.root).canonical(self._resolve_event_id(event_id) or event_id)
            members=Pins(self).sources(event_id)
            if members:
                originals=[self.read_source_event(eid,truncate=False,merge_sources=False) for eid in members]
                text='\n\n'.join(r.get('transcript','') for r in originals if r.get('transcript'))
                clipped=truncate and len(text)>self.READ_SOURCE_MAX_CHARS
                result={'status':'ok' if all(r.get('status')=='ok' for r in originals) else 'partial',
                    'event_id':event_id,'source_event_ids':members,'transcript':text[:self.READ_SOURCE_MAX_CHARS] if clipped else text,
                    'truncated':clipped}
                if not truncate:
                    result['messages']=[m for row in originals for m in (row.get('messages') or [m for item in row.get('slices',[]) for m in item['messages']])]
                return result
        explicit = read_memory(self, event_id, canonicalize=merge_sources)
        if explicit is not None:
            return explicit
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
                              "（写入早于定位功能或出处未回填）"}
        sid = sess.group(1)
        # Frozen evidence is the only authority for an old Event. A newer
        # session, max-reply ledger or current leaf cannot replace that source.
        ref = self.source_completions.enrich(event_reference(
            event, "expanded", self.event_sources, spans))
        if ref["source_evidence"] not in ("frozen_messages", "generation_completed"):
            return {"status": "not_archived", "event_id": resolved,
                    "reason": "source_path_unknown"}
        if str((self.event_sources.get(resolved) or {}).get("version_id")) != str(event.version_id):
            return {"status": "not_archived", "event_id": resolved,
                    "reason": "source_version_mismatch"}
        turns = sorted({t for lo, hi in ref["source_turn_spans"] for t in range(lo, hi + 1)})
        messages = {m["turn"]: m for m in ref["turn_messages"]}
        identities = []
        for turn in turns:
            item = messages.get(turn, {})
            for role, field in (("user", "user_message_id"), ("assistant", "assistant_message_id")):
                seq = message_seq(item.get(field))
                if seq is None:
                    return {"status": "not_archived", "event_id": resolved,
                            "reason": "source_seq_missing"}
                identities.append({"turn": turn, "role": role, "message_id": seq,
                                   "recall_id": item.get("source_recall_id")})
        context_expanded = False  # Neighbours have no frozen path evidence.
        try:
            rows = read_messages(sid, identities, self.source_instance)
        except source_client.SourceUnavailable:
            rows = []
        if not rows:
            return {"status": "not_archived", "event_id": resolved,
                    "reason": "该事件的原文轮次拉取不到"
                              "（后端原文接口未配置或不可达）"}
        transcript = "\n".join(
            f"[turn {r['turn']}] {r['role']}: {r['content']}" for r in rows)
        truncated = False
        if truncate and len(transcript) > self.READ_SOURCE_MAX_CHARS:
            transcript = transcript[:self.READ_SOURCE_MAX_CHARS]
            truncated = True
        return {
            "status": "ok",
            "event_id": resolved,
            "turns": turns,
            "context_expanded": context_expanded,
            **({"messages":rows} if not truncate else {}),
            "transcript": transcript,
            "truncated": truncated,
        }



    def _presented_event_refs(self, presentations: list[dict]) -> list[dict]:
        """Only actual event content; hidden entity members never become refs."""
        refs: dict[str, dict] = {}
        for item in presentations:
            kind = item["presentation"]
            eid = item.get("event_id")
            if kind not in ("event_text", "event_excerpt") or not eid:
                continue
            if eid in refs and refs[eid]["presentation"] == "event_text":
                continue
            event = self.store.get_event(eid)
            refs[eid] = event_reference(event, kind, self.event_sources,
                                        self.turn_spans.span_of(eid))
            refs[eid] = self.source_completions.enrich(refs[eid])
            from .knowledge import KnowledgeStore
            explicit_sources = KnowledgeStore(self.store.root,self.source_instance).event_sources(eid)
            if explicit_sources is not None:
                refs[eid]['source_slices'] = explicit_sources
        return list(refs.values())

    # 召回内联【当时对话】的单条上限（三席合计 ~3600 字，baseline 可调）
    _INLINE_SOURCE_MAX_CHARS = 1200

    # 内联判定的语义分数门槛（2026-10-19 实测标定）。首版 0.80 按短词
    # 标定（同义带 0.76~0.89）外推，被 momo 质疑后用**真实长句**重标：
    # 真相关（同义换词）0.47~0.73、半相关 0.44~0.59、无关 0.40~0.51
    # （Qwen3-Embedding-8B，5+4+5 句）——长句对短语粒度错配压平 ~0.15，
    # 0.80 永不触发。0.62 取半相关封顶 0.590 与真相关托底 0.645 之间
    # 的窄缝正中：非专名真相关 4/4 过线、半相关 0/4、无关 0/5。
    # 专名 cue（如 Deliz）同义换词天然弱（实测 0.471）——专名走词面
    # 路，语义路不救它是结构性的。样本仍小，渗透风险在（重叠区
    # 0.47~0.59），合取条件（sig 8-9 + 双线索）扛大头，本门槛只做
    # 第三道闸；样本大了重标（纪律同 CUE_SEMANTIC_FLOOR）。
    INLINE_SEMANTIC_FLOOR = 0.62

    def _inline_source_lines(self, eid: str, lines: list[str]) -> bool:
        """高匹配事件内联【当时对话】（2026-10-18 momo 定稿）。

        调用方负责三条件判定（sig 8-9 + 多线索共证 + 语义过值，
        要罕见——上下文撑不住）。本方法只管取材与降级：复用
        read_source_event（内存会话优先，缺的轮次按需拉后端，±3 轮
        场景扩展与工具读同源）；超长截断并留深挖提示。拉不到
        （未配置 source / 轮次缺失 / 无 span）→ 返回 False，调用方
        静默降级为原话行——内联是增强不是依赖，绝不挡召回。
        """
        try:
            src = self.read_source_event(eid)
        except Exception:  # noqa: BLE001 — 内联失败不挡召回注入
            return False
        transcript = str(src.get("transcript") or "").strip()
        if src.get("status") != "ok" or not transcript:
            return False
        more = ""
        if len(transcript) > self._INLINE_SOURCE_MAX_CHARS:
            transcript = transcript[:self._INLINE_SOURCE_MAX_CHARS]
            more = "\n  ……（更长原文可调用 read_memory_source）"
        lines.append("  【当时对话】")
        lines.append("\n".join("  " + ln
                               for ln in transcript.splitlines()) + more)
        return True

    @staticmethod
    def _format_key_quotes(event):
        return ""

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
            core=w_event["core"],
            significance=w_event["significance"],
            kind=w_event["kind"], focus=w_event["focus"],
            due=w_event.get("due"),
            provenance_refs=["memory-writer", f"chat:{session.session_id}"],
        )

    def _fetched_corpus(self, sid: str, turns: set[int] | None = None
                        ) -> list[dict[str, Any]]:
        """Read known raw identities on the anchored path, retaining safe diagnostics."""
        try:
            modern = generation_messages(self.generations, sid)
            modern_turns = {m["turn"] for m in modern}
            legacy = legacy_messages(self.turn_ledger, sid)
            candidates = modern + [m for m in legacy if m["turn"] not in modern_turns]
            session = self.sessions.get(sid)
            current = [m for m in session.messages if m.get("turn") == session.turn_counter
                       and m.get("role") in ("user", "assistant") and message_seq(m.get("_source_message_id"))] if session else []
            anchor = ({"turn": current[-1]["turn"], "role": current[-1]["role"],
                       "message_id": message_seq(current[-1]["_source_message_id"])} if current else None)
            rows = current_path_corpus(sid, candidates, anchor, self.source_instance)
            rows.issue = rows.issue or getattr(legacy, "issue", None)
        except source_client.SourceUnavailable as exc:
            return SourceCorpus(issue=exc.code)
        except OSError:
            return SourceCorpus(issue="source_unreachable")
        return SourceCorpus([m for m in rows if turns is None or m["turn"] in turns], rows.issue)

    def _quote_corpus(self, session: Session) -> list[dict[str, Any]]:
        """Use a provider-proven path plus the current real in-flight message.

        Legacy hooks without source identities still use
        their own ephemeral text; they cannot produce trusted historical refs.
        """
        fetched = self._fetched_corpus(session.session_id)
        if not any(m.get("_source_message_id") for m in session.messages):
            return SourceCorpus(session.messages + fetched, getattr(fetched, "issue", None))
        current = [m for m in session.messages if m.get("turn") == session.turn_counter]
        # Only the current user is known without a parent-chain read. Historical
        # local rows are not merged by turn number into a different branch.
        current = [m for m in current if m.get("role") == "user"]
        present = {m.get("_source_message_id") for m in fetched}
        return SourceCorpus(fetched + [m for m in current if m.get("_source_message_id") not in present],
                            getattr(fetched, "issue", None))

    @staticmethod
    def _validate_focus(event, corpus, spans=None):
        from .turn_span import locate_message
        messages=[m for m in corpus if spans is None or any(lo <= m.get('turn',-1) <= hi for lo,hi in spans)]
        message=locate_message(event.focus,messages)
        if message is None:
            raise QuoteSourceError('quote_not_found',field='event.focus')
        seq=message_seq(message.get('_source_message_id',message.get('message_id')))
        if seq is None:
            raise QuoteSourceError('source_identity_missing',field='event.focus')
        event.focus=seq





    # ---------- heart 联动 ----------

    # （旧梦通道 dream_material_preview / push_dream_night / 夜账本观察
    #   已于 2026-10-15 退役——新版设计里梦归 app 的 dream_submit 工具，
    #   ember 不再主动向 heart 推送；turn_ledger 观察保留）


    # ---------- 旁路服务（2026-10-12 拍板：MCP 工具 + 轮前/轮后钩子） ----------
    #
    # 接入形态（入口 sidecar.py）：
    #   轮前钩子 → 前端自行注入召回文本（Ember 不再改写任何请求）
    #   MCP 工具 → 模型经前端工具循环调用，只提供「写什么」；
    #              出处（会话/轮次/heart 钉子）由轮前钩子记下的
    #              进行中一轮自动关联——模型无需知道任何内部编号。
    #   轮后钩子 → 原文存档 + 收轮。
    # 设计约束：无感工具——上下文永远以前端为准；失败如实回执不编造。

    @staticmethod
    def _generation_error(code: str, error: str) -> dict:
        return {"ok": False, "status": "failed", "code": code, "error": error}

    def _reconcile_recall_effects(self, recall_id: str | None = None) -> None:
        for effect in self.generations.pending_effects(recall_id):
            entry = json.loads(effect["entry_json"])
            if effect["kind"] == "turn_start":
                self.turn_ledger.record_start_once(effect["operation_id"], entry)
            elif effect["kind"] == "recall":
                self.recall_ledger.record_once(effect["operation_id"], entry)
            elif effect['kind']=='pin_reminder':
                from .pin_policy import Pins
                Pins(self).acknowledge(entry['episode'])
            elif effect['kind']=='promise_exposure':
                from .promises import Promises
                Promises(self).enter(**entry)
            else:
                raise ValueError("Unknown recall effect kind")
            self.generations.acknowledge_effect(effect["operation_id"])
        self.source_completions.reconcile()

    @staticmethod
    def _recall_pending(recall_id: str, code: str = "recall_in_progress") -> dict:
        return {"status": "in_progress", "code": code, "recall_id": recall_id,
                "retry_after_sec": 2}

    def sidecar_recall_snapshot(self, recall_id: str, view: str = "refs") -> dict:
        if view not in ("refs", "response"):
            return self._generation_error("invalid_arguments", "view must be refs or response")
        row = self.generations.snapshot(recall_id)
        if row is None:
            code = "recall_snapshot_unavailable" if self.generations.generation(recall_id) else "unknown_recall"
            return self._generation_error(code, "No recall snapshot for this identity")
        if row["state"] == "in_progress":
            return self._recall_pending(recall_id)
        if row["state"] != "ready":
            return self._generation_error("recall_outcome_unknown", "Interrupted recall has no committed result; do not rerun")
        with self._generation_lock:
            try:
                self._reconcile_recall_effects(recall_id)
            except Exception:
                return self._recall_pending(recall_id, "recall_recovery_pending")
            row = self.generations.snapshot(recall_id)
            if view == "response" and row["response_json"] is None:
                return self._generation_error("response_expired", "Recall response expired; identity and refs remain")
            metadata = json.loads(row["metadata_json"])
            revision, candidates = self.source_completions.view(recall_id, metadata["recall_refs"]["events"])
            result = {"recall_id": recall_id, "session_id": metadata["session_id"],
                      "turn": metadata["turn"], "status": "ready", "snapshot": metadata,
                      "sources_revision": revision,
                      "source_candidates": candidates,
                      "response_available": row["response_json"] is not None,
                      "expires_at": datetime.fromtimestamp(row["expires_at"], timezone.utc).isoformat().replace("+00:00", "Z")}
            if view == "response":
                result["response"] = json.loads(row["response_json"])
            return result

    def _replay_recall(self, recall_id: str) -> dict:
        result = self.sidecar_recall_snapshot(recall_id, "response")
        return result["response"] if "response" in result else result

    def hook_turn_start(self, session_id: str | None, message: str,
                        heart_turn_id: str | None = None,
                        user_message_id: int | None = None,
                        context_start_id: int | None = None,
                        recall_id: str | None = None,
                        occurred_at: str | None = None,
                        activity_revision: int | None = None,
                        window_revision: int | None = None,
                        context_message_ids: list[int] | None = None) -> dict[str, Any]:
        """Persist the identity before work; commit response and effects together."""
        if context_message_ids is not None and (not isinstance(context_message_ids,list) or len(context_message_ids)>50000 or any(type(i) is not int or i<1 for i in context_message_ids)):
            return self._generation_error("invalid_arguments", "Invalid context message IDs")
        if recall_id is None:
            with self._generation_lock:
                try:
                    self._reconcile_recall_effects()
                except Exception:
                    return self._generation_error("ledger_recovery_pending", "Earlier recall effects need recovery")
                self.generations.supersede()
                self._generation_contexts.clear()
                result=self._hook_turn_start_legacy(
                    session_id, message, heart_turn_id, user_message_id, context_start_id,
                    context_message_ids)
                from .promises import Promises
                for item in result.get('presentations',[]):
                    if item.get('event_id'):
                        Promises(self).enter(item['event_id'],'cue',{'session_id':result.get('session_id'),
                            'message_ids':[str(user_message_id)] if user_message_id is not None else [],'turn':result.get('turn')})
                from .pin_policy import Pins
                budget=Pins(self).budget()
                result['system_reminders']=[budget['reminder']] if budget['reminder'] else []
                if budget['reminder']:
                    Pins(self).acknowledge(budget['episode'])
                return result
        rid, sid, seq = identity(recall_id), identity(session_id), message_seq(user_message_id)
        if rid is None or sid is None or seq is None or not message.strip():
            return self._generation_error("invalid_identity", "recall_id, session_id and a real user_message_id are required")
        if any(v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0)
               for v in (activity_revision, window_revision)):
            return self._generation_error("invalid_arguments", "Revisions must be nonnegative integers")
        if occurred_at is not None:
            try:
                stamp = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    raise ValueError("Timezone required")
            except (AttributeError, TypeError, ValueError):
                return self._generation_error("invalid_arguments", "occurred_at must be an RFC3339 timestamp with timezone")
        request_context = {"session_id": sid, "heart_turn_id": heart_turn_id,
            "user_message_id": seq, "context_start_id": context_start_id,
            "occurred_at": occurred_at, "activity_revision": activity_revision,
            "window_revision": window_revision}
        if context_message_ids is not None:
            request_context["context_message_ids"] = sorted(set(context_message_ids))
        semantic_context = {k: v for k, v in request_context.items()
                            if k not in ("occurred_at", "activity_revision", "window_revision") or v is not None}
        request_hash = fingerprint(dict(semantic_context, message=message))
        old = self.generations.generation(rid)
        if old is not None:
            if old["request_hash"] != request_hash:
                return self._generation_error("id_conflict", "recall_id content conflict")
            return self._replay_recall(rid)
        with self._generation_lock:
            old = self.generations.generation(rid)
            if old is not None:
                if old["request_hash"] != request_hash:
                    return self._generation_error("id_conflict", "recall_id content conflict")
                return self._replay_recall(rid)
            try:
                self._reconcile_recall_effects()
            except Exception:
                return self._generation_error("ledger_recovery_pending", "Earlier recall effects need recovery")
            session = self.get_or_create_session(sid)
            turn = max(session.turn_counter, self.generations.max_turn(sid)) + 1
            self.generations.reserve(rid, sid, request_hash, turn, {"request_context": request_context})
            session.turn_counter = turn
            self._generation_contexts.clear()
            self._live_turn = None
            key = (sid, turn)
            # Preview only the known real user seq. This preserves legacy window
            # filtering/source reads during computation without persisting a turn.
            self.turn_ledger.user_msg[key] = int(seq)
            try:
                entries: list[dict] = []
                rec = self.run_recall_pipeline(session, turn, message,
                    context_start_id=context_start_id, deferred_entries=entries,context_message_ids=context_message_ids)
                stats = {k: rec[k] for k in (
                    "recalled_count", "entity_recalled", "entity_names", "filtered_by_sleep",
                    "filtered_by_window", "resolved_brought", "semantic_matched", "semantic_reason")}
                refs = {"recall_id": rid, "session_id": sid, "anchor_message_id": seq,
                        "events": rec["recall_refs"]["events"]}
                out = {"injected": bool(rec["prefix"]), "injected_text": rec["prefix"] or None,
                       "session_id": sid, "turn": turn, "memory": stats, "recall_id": rid,
                       "presentations": rec["presentations"], "recall_refs": refs,
                       "slice_recall": rec.get("slice_recall")}
                metadata = {"session_id": sid, "turn": turn, "request_context": request_context,
                            "presentations": rec["presentations"], "recall_refs": refs,
                            "slice_recall": rec.get("slice_recall")}
                start_entry = {"ember_session": sid, "ember_turn": turn,
                               "user_message_id": int(seq), "recorded_at": _now_iso()}
                if heart_turn_id:
                    start_entry["heart_turn_id"] = str(heart_turn_id).strip()
                effects = [("turn_start", start_entry)] + [("recall", entry) for entry in entries]
                for p in out['presentations']:
                    eid=p.get('event_id')
                    if eid and self.store.get_event(eid).kind=='promise':
                        effects.append(('promise_exposure',{'event_id':eid,
                            'route':p.get('route') or ('recall' if p.get('via_slice_id') else 'cue'),
                            'span':{'session_id':sid,'message_ids':[seq],'recall_id':rid},'at':_now_iso()}))
                from .pin_policy import Pins
                pin_budget=Pins(self).budget()
                out['system_reminders']=[pin_budget['reminder']] if pin_budget['reminder'] else []
                if pin_budget['reminder']:
                    effects.append(('pin_reminder',{'episode':pin_budget['episode']}))
                self.generations.commit_snapshot(rid, turn, out, metadata, effects)
                self._reconcile_recall_effects(rid)
            except Exception:
                self.generations.interrupt(rid)
                snapshot = self.generations.snapshot(rid)
                if snapshot is not None and snapshot["state"] == "ready":
                    return self._recall_pending(rid, "recall_recovery_pending")
                return self._generation_error("recall_outcome_unknown", "Recall failed before a result commit; do not rerun this identity")
            finally:
                if key not in self.turn_ledger.all_turns:
                    self.turn_ledger.user_msg.pop(key, None)
            session.messages.append({"role": "user", "content": message, "turn": turn,
                "_source_session_id": sid, "_source_message_id": seq})
            self._generation_contexts[rid] = {"session": session, "turn": turn,"request_context":request_context}
            self._last_context=self._generation_contexts[rid]
            self._live_turn = {"session": session, "turn": turn, "user_text": message, "recall_id": rid}
            return out

    def hook_turn_end(self, session_id: str | None, assistant_reply: str,
                      turn: int | None = None,
                      assistant_message_id: int | None = None,
                      recall_id: str | None = None,
                      status: str = "done") -> dict[str, Any]:
        with self._generation_lock:
            if recall_id is None:
                if self._live_turn and self._live_turn.get("recall_id"):
                    return self._generation_error("identity_required", "recall_id is required for this generation")
                return self._hook_turn_end_legacy(session_id, assistant_reply, turn, assistant_message_id)
            rid = identity(recall_id)
            row = self.generations.generation(rid) if rid else None
            if row is None:
                return self._generation_error("unknown_recall", "Unknown generation")
            if session_id != row["session_id"] or (turn is not None and turn != row["turn"]):
                return self._generation_error("context_mismatch", "Generation session/turn mismatch")
            seq = message_seq(assistant_message_id)
            if status not in ("done", "error", "cancelled"):
                return self._generation_error("invalid_status", "Expected done, error or cancelled")
            if status == "done" and (not assistant_reply.strip() or seq is None):
                return self._generation_error("invalid_identity", "Completed dialogue needs real reply text and seq")
            request_hash = fingerprint({"session_id": session_id, "turn": row["turn"],
                "assistant_reply": assistant_reply, "assistant_message_id": seq, "status": status})
            if row["end_hash"] is not None:
                if row["end_hash"] != request_hash:
                    return self._generation_error("id_conflict", "Generation terminal content conflict")
                self._recover_source_completions()
                return json.loads(row["end_result"])
            if row["turn"] is None:
                return self._generation_error("generation_not_ready", "Generation never completed turn registration")
            # Completion is a historical fact, even after supersession/restart.
            # Never reopen a writer or append late text to the current context.
            ctx = self._generation_contexts.get(rid)
            if status == "done":
                self.turn_ledger.record_reply(session_id, row["turn"], int(seq))
            result = {"ok": True, "session_id": session_id, "turn": row["turn"],
                      "recall_id": rid, "status": status}
            self.generations.finish(rid, status, request_hash, result, seq if status == "done" else None)
            if status == "done" and ctx is not None and row["state"] == "running":
                ctx["session"].messages.append({"role": "assistant", "content": assistant_reply,
                    "turn": row["turn"], "_source_session_id": session_id,
                    "_source_message_id": seq, "_source_recall_id": rid})
            self._generation_contexts.pop(rid, None)
            if self._live_turn and self._live_turn.get("recall_id") == rid:
                self._live_turn = None
            self._recover_source_completions()
            return result

    def _recover_source_completions(self) -> None:
        try:
            self.source_completions.reconcile()
            from .event_slices import sync_from_files
            sync_from_files(self.store.root, self.source_instance)
        except Exception:
            # The accepted terminal receipt is the durable recovery journal.
            # Queries retry reconciliation and return pending while unavailable.
            pass

    def sidecar_generation_status(self, recall_id: str) -> dict:
        row = self.generations.generation(recall_id)
        if row is None:
            return self._generation_error("unknown_recall", "Unknown generation")
        return {"recall_id": recall_id, "session_id": row["session_id"],
                "turn": row["turn"], "status": row["state"],
                "superseded_by": row["superseded_by"],
                "assistant_message_id": row["assistant_message_id"],
                "calls": self.generations.calls_for(recall_id),
                "recall_snapshot_available": bool((snapshot := self.generations.snapshot(recall_id))
                    and snapshot["state"] == "ready" and snapshot["response_json"] is not None
                    and not self.generations.pending_effects(recall_id))}

    def _tool_receipt(self, row: dict) -> dict:
        if row["state"] != "completed":
            return self._generation_error("tool_outcome_unknown",
                "Accepted tool outcome is unknown/in progress; do not replay; reconcile the receipt")
        if row["volatile"]:
            cached = self._volatile_tool_results.get((row["recall_id"], row["tool_call_id"]))
            if cached is None:
                return self._generation_error("result_not_retained",
                    "Read result was not persisted; this identity cannot be re-executed")
            return copy.deepcopy(cached)
        return json.loads(row["result_json"])

    def sidecar_tool_status(self, recall_id: str, tool_call_id: str) -> dict:
        row = self.generations.call(recall_id, tool_call_id)
        if row is None or row["recall_id"] != recall_id:
            return self._generation_error("unknown_tool_call", "Unknown tool call in this generation")
        return {"recall_id": recall_id, "tool_call_id": tool_call_id,
                "tool_name": row["tool_name"], "status": row["state"],
                "result": self._tool_receipt(row)}

    def sidecar_run_start(self, body: dict) -> dict:
        return self.runs.start(body)

    def _verify_run_reply(self, start: dict, finish: dict) -> dict | None:
        reply_id = message_seq(finish["assistant_message_id"])
        messages = start["messages"]
        ids = [message_seq(row["message_id"]) for row in messages]
        if reply_id in ids:
            return run_failure("context_mismatch", "Reply seq is already in the start window")
        try:
            rows = source_client.fetch_source_path(start["session_id"], ids + [reply_id],
                                                   instance=self.source_instance)
        except source_client.SourceUnavailable as exc:
            result = run_failure("run_source_unavailable", "Reply verification pending; retry the same fixed finish intent")
            result["source_code"] = exc.code
            return result
        by_id = {message_seq(row.get("message_id")): row for row in rows if isinstance(row, dict)}
        if len(rows) != len(ids) + 1 or len(by_id) != len(rows):
            return run_failure("run_source_mismatch", "Source identities incomplete or repeated")
        if [message_seq(row.get("message_id")) for row in rows] != ids + [reply_id]:
            return run_failure("run_source_mismatch", "Verified source order does not match the attested window and reply")
        for item in messages:
            row = by_id.get(message_seq(item["message_id"]))
            if row is None or row.get("role") != item["role"]:
                return run_failure("run_source_mismatch", "Start path role/identity mismatch")
        reply = by_id.get(reply_id)
        if reply is None or reply.get("role") != "assistant" or reply.get("content") != finish["assistant_reply"]:
            return run_failure("run_source_mismatch", "Committed reply role/text mismatch")
        return None

    def sidecar_run_finish(self, body: dict) -> dict:
        return self.runs.finish(body, self._verify_run_reply)

    def sidecar_run_status(self, run_id: str) -> dict:
        return self.runs.query(run_id)

    def sidecar_call_tool(self, name: str, arguments: dict,
                          recall_id: str | None = None,
                          tool_call_id: str | None = None,
                          run_id: str | None = None) -> dict:
        """HTTP-provided identity only. No identified request falls back to live."""
        if run_id is not None:
            if recall_id is not None:
                return run_failure("context_mismatch", "Ordinary recall and active run identities cannot be mixed")
            if not isinstance(arguments, dict):
                return run_failure("invalid_arguments", "Tool arguments must be an object")
            return self.runs.call(run_id, tool_call_id, name, arguments, self._dispatch_memory_tool)
        if recall_id is None and tool_call_id is None:
            with self._generation_lock:
                if self._live_turn and self._live_turn.get("recall_id"):
                    return self._generation_error("identity_required", "Generation tool identity headers are required")
                return self._dispatch_memory_tool(name, arguments)
        rid, cid = identity(recall_id), identity(tool_call_id)
        if rid is None or cid is None:
            return self._generation_error("invalid_identity", "Both recall and tool-call identity headers are required")
        try:
            request_hash = fingerprint({"recall_id": rid, "name": name, "arguments": arguments})
        except (TypeError, ValueError):
            return self._generation_error("invalid_arguments", "Tool arguments must be finite JSON")
        with self._generation_lock:
            old = self.generations.call(rid, cid)
            if old is not None:
                if old["recall_id"] != rid or old["request_hash"] != request_hash:
                    return self._generation_error("id_conflict", "Tool identity content conflict")
                # Completed receipts are replayable after completion/supersession.
                return self._tool_receipt(old)
            row = self.generations.generation(rid)
            if row is None:
                return self._generation_error("unknown_recall", "Unknown generation")
            ctx = self._generation_contexts.get(rid)
            if row["state"] != "running" or ctx is None:
                return self._generation_error("stale_generation", "Generation ended, was superseded or lost its live context")
            self.generations.begin_call(rid, cid, request_hash, name)
            try:
                result = self._dispatch_memory_tool(name, arguments,
                    context=ctx, recall_id=rid, tool_call_id=cid)
                # A read transcript stays volatile, never in the durable receipt.
                from .knowledge_tools import VOLATILE_TOOLS
                volatile = name == READ_SOURCE_TOOL_NAME or name in VOLATILE_TOOLS
                self.generations.complete_call(rid, cid, result, volatile=volatile)
                if volatile:
                    self._volatile_tool_results[(rid, cid)] = copy.deepcopy(result)
                return result
            except Exception:
                # A memory JSONL write and its receipt are not one transaction.
                # Preserve uncertainty rather than creating duplicate effects.
                try:
                    self.generations.unknown_call(rid, cid)
                except Exception:
                    pass
                return self._generation_error("tool_outcome_unknown", "Tool outcome could not be committed; reconcile before retrying")

    def _dispatch_memory_tool(self, name: str, arguments: dict,
                              context: dict | None = None,
                              recall_id: str | None = None,
                              tool_call_id: str | None = None) -> dict:
        if name in ('list_memory_pins','merge_memory_events'):
            from .pin_policy import Pins
            try:
                with self._generation_lock:
                    return Pins(self).listing() if name=='list_memory_pins' else Pins(self).merge(arguments)
            except (ValueError,KeyError,TypeError) as exc:
                return {'status':'failed','code':'invalid_pin_operation','error':str(exc)}
        if name=='read_relationship_point':
            from .timeline import Timeline
            return Timeline(self).read_point(arguments.get('point',''))
        from .organization_tools import TOOL_NAMES as ORGANIZATION_TOOLS, dispatch as organization_dispatch
        if name in ORGANIZATION_TOOLS:
            return organization_dispatch(self,name,arguments)
        from .promises import TOOL_NAMES as PROMISE_TOOLS, dispatch as promise_dispatch
        if name in PROMISE_TOOLS:
            return promise_dispatch(self,name,arguments,context,recall_id)
        from .extraction import TOOL_NAMES as EXTRACTION_TOOLS, dispatch as extraction_dispatch
        if name in EXTRACTION_TOOLS:
            return extraction_dispatch(self,name,arguments)
        from .knowledge_tools import TOOL_NAMES, dispatch
        if name in TOOL_NAMES:
            return dispatch(self, name, arguments, context)
        if name in (TOOL_NAME, PROMISE_TOOL_NAME):
            validated,error=validate_writer_arguments(arguments,promise=name==PROMISE_TOOL_NAME)
            if error:
                return writer_error_result(error)
            if 'source_slice_ids' in arguments:
                from .knowledge_tools import write_event
                try:
                    return write_event(self,validated,context)
                except SupersessionError as exc:
                    return exc.as_result()
                except QuoteSourceError as exc:
                    return exc.as_result()
                except (ValueError,KeyError,TypeError) as exc:
                    return {'status':'failed','code':'invalid_memory_material','error':str(exc)}
            if context is not None:
                return self._create_memory_in_context(context["session"], context["turn"],
                    arguments, recall_id, tool_call_id)
            return self.sidecar_create_memory_event(arguments)
        if name == REVISE_TOOL_NAME:
            return self.sidecar_revise_memory_event(arguments,context)
        if name == READ_SOURCE_TOOL_NAME:
            return self.sidecar_read_memory_source(arguments.get("event_id") or "")
        if name == SEARCH_TOOL_NAME:
            return self.sidecar_search_memory(arguments)
        if name == PIN_TOOL_NAME:
            return self.sidecar_pin_memory(arguments)
        if name == UNPIN_TOOL_NAME:
            return self.sidecar_unpin_memory(arguments)
        return {"status": "failed", "error": f"未知工具: {name}"}

    def _hook_turn_start_legacy(self, session_id: str | None, message: str,
                        heart_turn_id: str | None = None,
                        user_message_id: int | None = None,
                        context_start_id: int | None = None,
                        context_message_ids: list[int] | None = None) -> dict[str, Any]:
        """轮前钩子：开新一轮 + 本地召回，返回注入文本。

        返回 {injected, injected_text, session_id, turn, memory}。
        injected_text 为前端本轮自行注入的 [记忆浮现] 前缀块（无命中
        为 null）。同时记录进行中的一轮供 MCP 写入工具关联出处；
        同会话连续两次 turn_start（重新生成/重发）覆盖旧上下文。

        user_message_id（可选，2026-10-14 去原文化）：本轮刺激源在后端
        消息树的行 id——turn_ledger 登记映射，Event 本体不存（铁律）。
        context_start_id（可选）：当前窗口起点（后端 message_id），驱动
        召回起点过滤（来源全在窗口内的候选滤掉）。
        """
        session = self.get_or_create_session(session_id)
        turn = session.next_turn()
        self.turn_ledger.record(session.session_id, turn, heart_turn_id,
                                user_message_id=user_message_id)
        rec = self.run_recall_pipeline(session, turn, message,
                                       context_start_id=context_start_id,
                                       context_message_ids=context_message_ids)
        session.messages.append({"role": "user", "content": message, "turn": turn,
                                 "_source_session_id": session.session_id,
                                 "_source_message_id": message_seq(user_message_id)})
        self._live_turn = {"session": session, "turn": turn,
                           "user_text": message,"request_context":{"context_start_id":context_start_id,"user_message_id":user_message_id}}
        if context_message_ids is not None:
            self._live_turn["request_context"]["context_message_ids"] = sorted(set(context_message_ids))
        self._last_context=self._live_turn
        stats = {k: rec[k] for k in (
            "recalled_count", "entity_recalled", "entity_names",
            "filtered_by_sleep", "filtered_by_window", "resolved_brought",
            "semantic_matched", "semantic_reason")}
        return {
            "injected": bool(rec["prefix"]),
            "injected_text": rec["prefix"] or None,
            "session_id": session.session_id,
            "turn": turn,
            "memory": stats,
            "presentations": rec["presentations"],
            "slice_recall": rec.get("slice_recall"),
            "recall_refs": {"session_id": session.session_id,
                            "anchor_message_id": message_seq(user_message_id),
                            "events": rec["recall_refs"]["events"]},
        }

    def _hook_turn_end_legacy(self, session_id: str | None,
                      assistant_reply: str,
                      turn: int | None = None,
                      assistant_message_id: int | None = None) -> dict[str, Any]:
        """轮后钩子：登记最终回复与来源编号，并收掉进行中的一轮。

        turn 校验（防串轮竞态）：用户连发两条时，前一轮的 turn_end 可能
        晚于后一轮的 turn_start 到达——对不上号的上报拒收（stale），
        不污染新一轮的出处账。缺轮后钩子时不登记最终回复；turn 计数允许留洞。

        assistant_message_id（可选，2026-10-14 去原文化）：本轮回复在后端
        消息树的行 id——turn_ledger 补记（record_reply），供召回起点
        过滤与原文区间换算使用。
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
                                 "turn": live["turn"],
                                 "_source_session_id": session.session_id,
                                 "_source_message_id": message_seq(assistant_message_id)})
        try:
            self.turn_ledger.record_reply(session.session_id, live["turn"],
                                          assistant_message_id)
        except OSError:
            pass
        self._live_turn = None
        return {"ok": True, "session_id": session.session_id,
                "turn": live["turn"]}

    def sidecar_tool_specs(self) -> list[dict[str, Any]]:
        """MCP tools/list 工具清单（含修正、检索与钉住）。"""
        from .knowledge_tools import tool_specs
        from .promises import tool_specs as promise_specs
        from .organization_tools import tool_specs as organization_specs
        from .timeline import tool_specs as timeline_specs
        from .pin_policy import tool_specs as pin_specs
        from .extraction import tool_specs as extraction_specs
        return (build_tools_spec() + [build_revise_tool_spec()[0]]
                + build_search_tool_spec() + build_pin_tool_specs()
                + tool_specs() + promise_specs() + organization_specs() + timeline_specs() + pin_specs() + extraction_specs())

    def sidecar_create_memory_event(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """MCP 工具 create_memory_event 执行体：进行中的一轮 + 参数 → 落库。

        无进行中的一轮 → 如实回错（不打无出处之账）；校验失败 → failed
        回执（使用工具字段的统一 validate 规则）。
        """
        live = self._live_turn
        if live is None:
            return {"status": "failed",
                    "error": "当前没有进行中的对话轮（轮前钩子未调用），"
                             "无从记录这条记忆的出处"}
        if live.get("recall_id"):
            return self._generation_error("identity_required", "Use the bound MCP generation context")
        try:
            return self._create_memory_in_context(live["session"], live["turn"], arguments)
        except OSError as ex:
            return {"status": "failed", "error": str(ex)[:300]}

    def _create_memory_in_context(self, session: Session, turn: int, arguments: dict,
                                  recall_id: str | None = None,
                                  tool_call_id: str | None = None) -> dict:
        validated, err = validate_writer_arguments(arguments)
        if err:
            return writer_error_result(err)
        try:
            return self.write_validated_event(session, turn, validated,
                writer_recall_id=recall_id, writer_tool_call_id=tool_call_id)
        except QuoteSourceError as ex:
            return ex.as_result()
        except SupersessionError as ex:
            return ex.as_result()
        except (ValueError, KeyError) as ex:
            return {"status": "failed", "error": str(ex)[:300]}

    # ---------- 修改记忆（冷静期修正，2026-10-14 momo 拍板） ----------

    _SLEEP_PROGRESS_FILE = "sleep_progress.jsonl"

    def _sleep_done_ids(self) -> set[str]:
        """已进入过夜间维护的 event_id 集合（sleep_progress.jsonl，可重建）。"""
        p = os.path.join(getattr(self.store, "root", ""),
                         self._SLEEP_PROGRESS_FILE)
        done: set[str] = set()
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        if d.get("event_id"):
                            done.add(d["event_id"])
                    except json.JSONDecodeError:
                        continue
        return done

    def _normalize_revise_list(self, value: Any) -> list[str]:
        """list 字段归一：str → [str]（单值更正为主），list 原样收。"""
        if isinstance(value, list):
            return [str(v) for v in value]
        return [str(value)]

    def sidecar_revise_memory_event(
            self, arguments: dict[str, Any], context=None) -> dict[str, Any]:
        """MCP 工具 revise_memory_event 执行体：冷静期内直改本尊。

        冷静期 = 该 event 尚未进入过夜间维护（sleep_progress 无记录）——
        sleep 派生物（向量/关系/实体归属）尚未建立，改本尊无旧引用要追；
        过冷静期拒绝并指路 resolves（写更正事件，sleep 判联自动压旧）。
        字段白名单：叙述类可改（participants/objects/details 接受单值或
        数组）；出处/摘录/时间坐标锁死（动了双盲核对废、回指断）。
        无留痕（改了就改了，momo 裁决）；存储层面为 append 覆盖（加载时
        新行盖旧行），不加任何 revisions 字段。
        """
        from .models import RecallRoute
        from .writer import REVISE_ALLOWED_FIELDS, REVISE_LOCKED_FIELDS

        eid = str((arguments or {}).get("event_id") or "").strip()
        resolved = self._resolve_event_id(eid) if eid else None
        if resolved is None:
            return {"status": "failed",
                    "error": f"找不到该 event_id: {eid}"}
        from .memory_lifecycle import Lifecycle
        resolved=Lifecycle(self.store.root).canonical(resolved)
        event = self.store.get_event(resolved)

        from .event_mutation import visible, COOLDOWN_HINT
        if not visible(self,event,context):
            return {'status':'rejected','code':'cooldown_expired','event_id':resolved,'reason':COOLDOWN_HINT,'hint':COOLDOWN_HINT}

        # 锁死字段显式拒绝（防模型绕过白名单）
        for locked in REVISE_LOCKED_FIELDS:
            if locked in (arguments or {}):
                return {"status": "failed", "event_id": resolved,
                        "error": f"字段 {locked} 不可修改（与原文/系统的绑定）"}

        updates={k:arguments[k] for k in REVISE_ALLOWED_FIELDS if k in arguments}
        candidate=dict(event.to_dict(),**updates)
        if any(not isinstance(candidate[k],str) or not candidate[k].strip() for k in ('summary','core')):
            return {'status':'failed','code':'invalid_tool_arguments','error':'summary and core must be nonempty strings'}
        if type(candidate['significance']) is not int or not 1<=candidate['significance']<=10:
            return {'status':'failed','code':'invalid_tool_arguments','field':'significance'}
        if candidate['kind'] not in ('daily','relationship','project','promise') or ((candidate['kind']=='promise') != (event.kind=='promise')):
            return {'status':'failed','code':'invalid_tool_arguments','field':'kind'}
        revised=list(updates)
        for key,value in updates.items():
            setattr(event,key,value)

        # 召回线索整体重设（可选）：旧 active routes 退役，按新 cues 重建
        cues = (arguments or {}).get("recall_cues")
        if isinstance(cues, list) and cues:
            new_cues = [str(c).strip() for c in cues if str(c).strip()]
            if new_cues:
                for route in list(self.store._routes.values()):  # noqa: SLF001
                    if route.event_id == resolved and route.status == "active":
                        route.status = "retired"
                        self.store.write_route(route)
                for i, cue in enumerate(new_cues):
                    self.store.write_route(RecallRoute(
                        route_id=f"rt_{resolved}_rv{i + 1:02d}",
                        event_id=resolved,
                        matcher="lexical",
                        cue_type="lexical",
                        cue=cue,
                        status="active",
                        evidence_refs=[event.raw_ref],
                        provenance_refs=["memory-revise"],
                    ))
                revised.append("recall_cues")

        if not revised:
            return {"status": "failed", "event_id": resolved,
                    "error": "没有提供任何可修改字段"
                             f"（可改: {', '.join(REVISE_ALLOWED_FIELDS)}"
                             "，或 recall_cues）"}

        from .organization import Organization
        with Organization(self).db() as conn:
            conn.execute('DELETE FROM processed WHERE event_id=?',(resolved,))
        self.store.write_event(event)  # append 覆盖：加载时新行盖旧行
        self.store.rebuild_index()
        return {"status": "revised", "event_id": resolved,
                "revised_fields": revised}

    def sidecar_read_memory_source(self, event_id: str) -> dict[str, Any]:
        """MCP 工具 read_memory_source 执行体（只读，共用来源核验）。"""
        return self.read_source_event(event_id,truncate=False)

    # ---------- 共享Event行级维护 ----------

    def _patch_event_rows(self, updates: dict[str, dict],
                          whitelist: tuple[str, ...] = ("change",
                                                        "entity_refs")) -> None:
        """events.jsonl 行级白名单回填（change / entity_refs）。

        Event dataclass 不含 entity_refs（jsonl 侧字段），write_event 会
        丢——与 sleep_v2.rewrite_events 同法：读行合并重写再重载内存。
        """
        if not updates:
            return
        p = self.store._path(EVENTS_FILE)  # noqa: SLF001
        rows: list[dict] = []
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        rows.append(json.loads(line))
        for row in rows:
            eid = row.get("event_id") or ""
            up = updates.get(eid)
            if up is None:
                short = "-".join(eid.split("-")[:2])
                up = next((v for k, v in updates.items()
                           if k == short or k.startswith(short + "-")), None)
            if not up:
                continue
            for k in whitelist:
                if k in up:
                    row[k] = up[k]
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, p)
        self.store.reload()
        from .recall import _EVENT_ENTITY_REFS
        for eid, up in updates.items():
            if "entity_refs" in up:
                _EVENT_ENTITY_REFS[eid] = list(up["entity_refs"])


    # ---------- search_memory（2026-10-15 设计夜：主动检索=捞取） ----------
    #
    # 自动浮现之外的另一条路：浮起有竞争有衰减，捞起无竞争无衰减——
    # 全库可达（含 faded/superseded），捞出来如实标注状态。
    # 检索即提取：命中即记账（route="search"），刷新一切计时器，
    # 并即时解除该事件的 cue 收紧（重新被想起=重新获得浮现能力）。

    SEARCH_K_DEFAULT = 3
    SEARCH_K_MAX = 5
    FADED_DAYS = 30.0   # 收紧后满 30 天未提取 → 淡忘（baseline）

    @staticmethod
    def _clamp_k(raw: Any) -> tuple[int, str | None]:
        """k 钳制（2026-10-15 momo 拍板 3/5）：search 是定位器不是资料
        阅读器，超限说明 query 太泛——guidance 会教缩小范围。"""
        try:
            k = int(raw)
        except (TypeError, ValueError):
            return EmberService.SEARCH_K_DEFAULT, None
        if k > EmberService.SEARCH_K_MAX:
            return EmberService.SEARCH_K_MAX, f"k={k} 已按上限 5 返回"
        return (k, None) if k > 0 else (EmberService.SEARCH_K_DEFAULT, None)

    @staticmethod
    def _parse_time_range(expr: str) -> tuple[datetime, datetime] | None:
        """time_range 表达 → (start, end) 本地时间。支持 ISO 起止（..或至
        分隔）、单日、今天/昨天/前天/本周/上周/本月、最近N天/近N天/N天前/
        近两周。解析失败返回 None（调用方如实回执）。"""
        expr = (expr or "").strip()
        if not expr:
            return None
        now = datetime.now(timezone.utc).astimezone()
        day = timedelta(days=1)
        rel = {"今天": (now, now), "昨天": (now - day, now - day),
               "前天": (now - 2 * day, now - 2 * day),
               "本周": (now - timedelta(days=now.weekday()), now),
               "上周": (now - timedelta(days=now.weekday() + 7),
                        now - timedelta(days=now.weekday() + 1)),
               "本月": (now.replace(day=1), now),
               "最近两周": (now - timedelta(days=14), now),
               "近两周": (now - timedelta(days=14), now)}
        m = re.match(r"^(?:最近|近|上)?(\d+)\s*天(?:前)?$", expr)
        if m:
            start = now - timedelta(days=int(m.group(1)))
            return start, now
        if expr in rel:
            return rel[expr]
        for sep in ("..", "至", "~"):
            if sep in expr:
                a, b = expr.split(sep, 1)
                try:
                    s = datetime.fromisoformat(a.strip()).astimezone()
                    e = datetime.fromisoformat(b.strip()).astimezone()
                except ValueError:
                    return None
                return min(s, e), max(s, e)
        try:   # 单日
            d = datetime.fromisoformat(expr).astimezone()
            return d, d
        except ValueError:
            return None

    def _event_idle_days(self, event_id: str) -> float | None:
        """事件的遗忘时钟：now − max(原起算点, 最后提取时间)。

        原起算点与收紧同源（close 时刻/写入时刻）；最后提取时间取自
        账本（route 不限）。open 实体成员返回 None（豁免，无时钟）。
        """
        try:
            ev = self.store.get_event(event_id)
        except KeyError:
            return None
        ent_id = _event_entity_of(self.store, event_id)
        if ent_id:
            try:
                ent = self.entity_store.get(ent_id)
            except KeyError:
                return None
            if ent.status == "open":
                return None
            raw = ent.closed_at
        else:
            raw = ev.ingest_time
        try:
            anchor = datetime.fromisoformat(raw).astimezone()
        except (TypeError, ValueError):
            return None
        last_at = self.recall_ledger.last_extracted.get(event_id) or ""
        if last_at:
            try:
                last_dt = datetime.fromisoformat(last_at).astimezone()
                anchor = max(anchor, last_dt)
            except ValueError:
                pass
        return (datetime.now(timezone.utc).astimezone()
                - anchor).total_seconds() / 86400.0

    def is_faded(self, event_id: str) -> bool:
        """淡忘：有 cue 且全部收紧，且遗忘时钟满 30 天未提取。

        现算谓词——零存储，提取即自动解除（计时器刷新+restore）。
        无 cue 的对象（观察条目）不归 faded 管。
        """
        routes = self.store.routes_for_event(event_id)
        if not routes or any(r.status == "active" for r in routes):
            return False
        idle = self._event_idle_days(event_id)
        return idle is not None and idle > self.FADED_DAYS

    def _restore_event_cues(self, event_id: str) -> int:
        """解除某事件的全部 cue 收紧（提取即 remembering）。返回条数。"""
        return sum(1 for r in self.store.routes_for_event(event_id)
                   if self.store.restore_route(r.route_id))

    def sidecar_search_memory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """MCP 工具 search_memory 执行体：捞取（全库可达，如实标注）。"""
        args = arguments or {}
        query = str(args.get("query") or "").strip()
        if not query:
            return {"status": "failed",
                    "error": "query 必填：自然语言描述要找的内容"}
        k, k_note = self._clamp_k(args.get("k"))
        tr = self._parse_time_range(str(args.get("time_range") or ""))
        if args.get("time_range") and tr is None:
            return {"status": "failed",
                    "error": ("time_range 解析失败：用「昨天/上周/最近3天」"
                              "等相对表达或 ISO 起止（a..b）")}
        notes: list[str] = []
        if k_note:
            notes.append(k_note)

        # ---- 主模态：core 向量余弦；兜底：LIKE（embed 不可达）----
        scored: list[tuple[str, float]] = []
        try:
            from .memory_maintenance import content_query
            content_rows = self.core_vectors.all() or self.entity_vectors.all()
            first_vl = next((r for r in content_rows if r.get('model','').startswith('vl:')),None)
            q_vec = content_query(first_vl,query,None,{}) if first_vl else embed_text(query)
        except Exception:  # noqa: BLE001 — 基础设施失败降级关键词
            q_vec = None
        in_range: dict[str, bool] = {}
        events = {ev.event_id: ev for ev in self.store.all_events()}
        if q_vec is not None:
            from .memory_maintenance import content_query
            content_queries = {first_vl['model']:q_vec} if first_vl else {}
            for row in self.core_vectors.all():
                candidate_query = content_query(row,query,None if first_vl else q_vec,content_queries)
                if candidate_query is None or len(candidate_query)!=len(row['vector']):
                    continue
                sim = cosine(candidate_query, row["vector"])
                if sim >= SEARCH_HIT_FLOOR:
                    scored.append((row["event_id"], sim))
            scored = [(eid, s) for eid, s in scored if eid in events]
            scored.sort(key=lambda x: x[1], reverse=True)
            pool = scored
        else:
            # LIKE 兜底：query 拆词（空格/逗号），任一词命中即算，
            # 按命中词数排序——降级模式下多关键词是排序信号
            terms = [t for t in re.split(r"[，,、\s]+", query)
                     if len(t.strip()) >= 2] or [query.lower()]
            scored_pool: list[tuple[str, int]] = []
            for eid, ev in events.items():
                blob = f"{ev.core or ''}\n{ev.summary or ''}".lower()
                hits = sum(1 for t in terms if t.lower() in blob)
                if hits:
                    scored_pool.append((eid, hits))
            scored_pool.sort(key=lambda x: (-x[1], x[0]))
            pool = scored_pool
            notes.append("向量服务不可达，已降级关键词检索（多关键词取并集）")

        # ---- 时间过滤（ingest_time 落区间）----
        if tr is not None:
            start, end = tr

            def _in(eid: str) -> bool:
                if eid in in_range:
                    return in_range[eid]
                raw = events[eid].ingest_time
                try:
                    t = datetime.fromisoformat(raw).astimezone()
                    ok = start <= t <= end + timedelta(days=1)
                except (TypeError, ValueError):
                    ok = False
                in_range[eid] = ok
                return ok

            pool = [(eid, s) for eid, s in pool if _in(eid)]

        total = len(pool)
        top = pool[:k]
        if total > k:
            notes.append(f"共 {total} 条相关，仅显示最相关的前 {k} 条")

        # ---- 结果行（状态标注如实）----
        items: list[dict[str, Any]] = []
        ent_ids: list[list[str]] = []
        restored_total = 0
        for eid, sim in top:
            ev = events[eid]
            item: dict[str, Any] = {
                "event_id": eid, "summary": ev.summary or "",
                "ingest_time": ev.ingest_time,
            }
            status = "normal"
            if self.sleep_state.status_of(eid) == "superseded":
                status = "superseded"
                res = self.relation_store.resolvers_of(eid)
                if res:
                    item["superseded_by"] = [r.dst_event for r in res]
            elif self.is_faded(eid):
                status = "faded"
            if ev.merged_into:
                status="merged"
                item["merged_into"]=ev.merged_into
            item["status"] = status
            refs = _event_entity_of(self.store, eid)
            item["entity_id"] = refs or ""
            ent_ids.append([refs] if refs else [])
            if status == "faded":
                restored_total += self._restore_event_cues(eid)
            items.append(item)
        if restored_total:
            notes.append(f"已唤醒 {restored_total} 条淡忘记忆的召回入口")

        # ---- 实体卡（印象层先出；recent 不复读 items 已展示的成员）----
        entity_cards: list[dict[str, Any]] = []
        hit_ids = [it["event_id"] for it in items]
        try:
            q_vec2 = q_vec
            if q_vec2 is None:
                q_vec2 = embed_text(query)
            from .memory_maintenance import content_query
            entity_queries = {first_vl['model']:q_vec} if first_vl and q_vec is not None else {}
            for row in self.entity_vectors.all():
                candidate_query = content_query(row,query,None if first_vl else q_vec2,entity_queries)
                if candidate_query is None or len(candidate_query)!=len(row['vector']):
                    continue
                sim = cosine(candidate_query, row["vector"])
                if sim < SEARCH_HIT_FLOOR:
                    continue
                try:
                    ent = self.entity_store.get(row["entity_id"])
                except KeyError:
                    continue
                members = [ev for ev in events.values()
                           if _event_entity_of(self.store,
                                               ev.event_id) == ent.entity_id]
                members.sort(key=lambda e: e.ingest_time, reverse=True)
                others = [m for m in members if m.event_id not in hit_ids]
                entity_cards.append({
                    "entity_id": ent.entity_id, "name": ent.name,
                    "status": ent.status, "summary": ent.summary or "",
                    "member_count": len(members),
                    "recent": [{"event_id": m.event_id,
                                "summary": m.summary or ""}
                               for m in others[:3]],
                })
        except Exception:  # noqa: BLE001 — 实体卡失败不拦检索结果
            notes.append("实体印象检索暂不可达")
        entity_cards.sort(key=lambda c: c["name"])

        # ---- 命中形态判定（旧版 R10 平移：chain=共享同一实体）----
        if not items:
            relation, guidance = "none", ("没有找到相关记忆。可换个说法再查，"
                                          "或如实告知对方。")
        elif len(items) == 1:
            relation, guidance = "single", "清单里只有一条。判断是否就是要找的那件事；要看细节用 read_memory_source 下钻。"
        else:
            common: set[str] | None = None
            for ids in ent_ids:
                s = set(ids)
                common = s if common is None else (common & s)
            if common:
                relation, guidance = "chain", ("这几条同属一个实体的经历链。"
                                               "需要整体经过或某次细节，"
                                               "按 event_id 用 "
                                               "read_memory_source 下钻。")
            else:
                relation, guidance = "cluster", ("多条相似候选。呈现你已经想起"
                                                 "的部分，先自行判断是否相关，"
                                                 "相关但缺失需要的细节可选择向"
                                                 "对方询问细节缩小范围；缩小到"
                                                 "具体哪条后可用 read_memory_"
                                                 "source 查看完整细节；无关或"
                                                 "是缩小不了就如实说记不清。")

        # ---- 检索即提取：记账 + 计时器刷新 ----
        if hit_ids:
            self.recall_ledger.record("mcp", 0, "search", hit_ids,
                                      via=f"query={query[:40]}")

        out: dict[str, Any] = {
            "status": "ok", "relation": relation, "guidance": guidance,
            "items": items, "total": total,
            "entity_cards": entity_cards,
        }
        if notes:
            out["note"] = "；".join(notes)
        return out


    # ---------- 简报（2026-10-17 设计夜定稿：截断驱动，拼装式零 LLM） ----------
    #
    # 丢头留尾式截断：app 每次截断时调 briefing/refresh——旧简报随头部
    # 丢弃，新简报必须自包含。候选池与层次（momo 逐项拍板）：
    #   pin + significance 10         必进，原文层（read_source ±3 轮）
    #   72h 新增（截掉区间里写的）     都要；8-9 → core，1-7 → summary；
    #                                 预算不够从低往高砍（pin/10 永不砍）
    #   召回过的旧记忆                 只放最相关一条（被召回次数最多，
    #                                 平局取最近被召回）→ core 层
    #   现场尾部（keep_from 起）        不进——原文还在上下文里
    # 去重：类间同一事件只出现一次取最高层；简报注入记一次在场
    # （ledger route="briefing"）→ 2h 内召回对同会话走轻量行；钉住
    # 永久不自动浮现（召回管线过滤，search 不受影响）。观察事件不进
    # （习惯不是「正在进行」）。层次依据 significance = 写入时前端 AI
    # 标的 "N：理由"（1-10）；无数字前缀的旧事件按 5 计（summary 层）。

    BRIEFING_FILE = "briefing_latest.json"

    def _briefing_path(self) -> str:
        return os.path.join(self.store.root, self.BRIEFING_FILE)

    @staticmethod
    def _sig_level(significance: int) -> int:
        return significance if type(significance) is int and 1 <= significance <= 10 else 5

    @staticmethod
    def _briefing_when(ev: Any) -> str:
        """Every entry explicitly labels its stored date or why it is unavailable."""
        raw = getattr(ev, "event_time", None)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return "时间缺失"
        if not isinstance(raw, str):
            return "时间解析失败"
        try:
            dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return "时间解析失败"
        return f"日期：{dt:%Y-%m-%d}"

    def _briefing_source_block(self, ev: Any) -> str:
        from .pin_policy import Pins
        return Pins(self).event_block(ev)

    def sidecar_briefing_refresh(self, args: dict[str, Any]) -> dict[str, Any]:
        from .briefing import refresh
        return refresh(self,args)

    def sidecar_briefing_get(self) -> dict[str, Any]:
        from .briefing import get
        return get(self)

    def sidecar_briefing_fallback(self, args: dict[str, Any]) -> dict[str, Any]:
        from .briefing_fallback import fallback
        return fallback(self, args)

    def sidecar_pin_memory(self, args: dict[str, Any]) -> dict[str, Any]:
        from .pin_policy import Pins
        try:
            with self._generation_lock:
                return Pins(self).pin(str(args.get('event_id') or ''))
        except (ValueError,KeyError) as exc:
            return {'status':'failed','error':str(exc)}

    def sidecar_unpin_memory(self, args: dict[str, Any]) -> dict[str, Any]:
        from .pin_policy import Pins
        try:
            with self._generation_lock:
                return Pins(self).unpin(str(args.get('event_id') or ''))
        except (ValueError,KeyError) as exc:
            return {'status':'failed','error':str(exc)}
