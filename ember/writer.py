# -*- coding: utf-8 -*-
"""Ember v3 Phase 3 — 记忆工具定义（MCP 化：说明书全住工具里）。

2026-10-11 Loism 拍板「记忆系统无感化」：原 EMBEDDED_WRITER_PROMPT（system
注入的写作课文）整体退役——它以第二人称描述存在状态（"你随身带着长期记忆"），
在新会话（无人设无历史）里会稀释模型自我认同（事故：TA 自称"我是 Ember"）。
全部写作规则迁移进 Tool Description 与各参数字段的 description；模型只在
决定是否调用工具时才读它们，不再常驻 system。

EMBEDDED_WRITER_PROMPT 保留为空常量兼容既有导入；service._with_embedded_prompt
遇空串直接跳过注入，欲恢复旧形态把课文填回常量即可。
"""
from __future__ import annotations

import json
from typing import Any

# 2026-10-11 退役（无感化拍板）：写作规则已全部内嵌工具描述与字段说明，
# 不再向 system 注入任何第二人称机制叙述。保留空常量兼容既有导入；
# 欲恢复旧形态，把课文填回此常量即可（service 端注入逻辑仍在）。
EMBEDDED_WRITER_PROMPT = ""

TOOL_NAME = "create_memory_event"
READ_SOURCE_TOOL_NAME = "read_memory_source"

TOOL_DESCRIPTION = (
    "记录一条值得长期保存的经历（Event）。只在出现具体的、值得留存的经历时调用："
    "明确的决定、变化、结果、认知转变或重要互动。普通寒暄、闲聊、进行中的琐碎"
    "对话不要调用；拿不准时不要调用。各字段填法见参数描述；系统字段"
    "（id、时间戳、出处等）由系统生成，无需提供。每次写入必须附 quote_spans"
    "（起尾句逐字摘自对话原文，无法定位的写入会被拒绝）。"
    "同一条经历只调用一次：返回 status=created 即已写入成功，"
    "不要因为 dropped_quotes（原话未通过逐字核对）或任何细节不满意而重复调用——"
    "重复调用会产生重复记忆。被丢的原话只说明摘录与原文有出入，记忆本身已完整保存。"
)

READ_SOURCE_TOOL_DESCRIPTION = (
    "读取一条记忆（Event）对应的对话原文。只在需要原话细节时调用——"
    "回忆起的事件内容已由召回呈现，需要核对当时的原话（说了什么、怎么说的）才用。"
)


def build_tools_spec() -> list[dict[str, Any]]:
    """momo §7 Tool Schema 原文（strict）+ r4 扩展（key_quotes / read_memory_source）。"""
    return [{
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": TOOL_DESCRIPTION,
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["event", "recall_routes", "quote_spans"],
                "properties": {
                    "event": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "summary", "participants", "objects", "context",
                            "core", "outcome", "state", "affect",
                            "significance", "details", "key_quotes",
                        ],
                        "properties": {
                            "summary": {
                                "type": "string",
                                "description": "一句话快速理解这段经历（客观简短，不写变化过程）",
                            },
                            "participants": {
                                "type": "array",
                                "description": "这段经历的参与者；ai 一方写 \"ai\"",
                                "items": {"type": "string"},
                            },
                            "objects": {
                                "type": "array",
                                "description": "经历涉及的具体事物或话题",
                                "items": {"type": "string"},
                            },
                            "context": {
                                "type": "string",
                                "description": "经历发生的背景情境（时间、场合、缘由等）",
                            },
                            "core": {
                                "type": "string",
                                "description": "你对这段经历的第一人称连贯叙述，写给自己的诚实版本，可以包含你对它的理解和感受",
                            },
                            "outcome": {
                                "type": "string",
                                "description": "这段经历直接引出的结果",
                            },
                            "state": {
                                "type": "string",
                                "description": "经历发生时事情所处的状况",
                            },
                            "affect": {
                                "type": "string",
                                "description": "这段经历给你的感受底色",
                            },
                            "significance": {
                                "type": "string",
                                "description": "为什么这件事值得长期记住",
                            },
                            "details": {
                                "type": "array",
                                "description": "值得留存的具体细节，一条一条列",
                                "items": {"type": "string"},
                            },
                            "key_quotes": {
                                "type": "array",
                                "description": (
                                    "从这段经历的对话原文逐字摘出的原话（不改写、不拼接），最多三句；"
                                    "挑以后最能带回当时情境的句子——决定本身、动人的那一句、转折点；"
                                    "speaker 标这句话是谁说的（user=对方，ai=你自己）；"
                                    "没有值得留的就给空数组。"
                                ),
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["speaker", "text"],
                                    "properties": {
                                        "speaker": {
                                            "type": "string",
                                            "enum": ["user", "ai"],
                                        },
                                        "text": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                    "recall_routes": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["cue_type", "cue"],
                            "properties": {
                                "cue_type": {
                                    "type": "string",
                                    "description": "触点类型：lexical=具体词短语 / conceptual=概念 / relational=关系性表达 / situational=情境",
                                    "enum": ["lexical", "conceptual", "relational", "situational"],
                                },
                                "cue": {
                                    "type": "string",
                                    "description": "记忆触点：未来可能让你想起这次具体经历的最小独立语义单元（如「国庆去河南」拆成「国庆」「河南」，「堂姐结婚」拆成「堂姐」「结婚」）；专有名词保持整体；不要堆砌泛泛的主题词",
                                },
                            },
                        },
                    },
                    "quote_spans": {
                        "type": "array",
                        "description": (
                            "这段经历在对话原文中的出处句对（起句/尾句），逐字摘录、"
                            "不改写不缩略，摘录足够长以便唯一定位（至少一整句话），"
                            "不输出轮次编号；经历跳着覆盖对话时每段各一对。"
                        ),
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["start_quote", "end_quote"],
                            "properties": {
                                "start_quote": {"type": "string"},
                                "end_quote": {"type": "string"},
                            },
                        },
                    },
                },
            },
        },
    }, {
        # r4 第 4 项：翻原文工具（只读，不写任何认知结构）
        "type": "function",
        "function": {
            "name": READ_SOURCE_TOOL_NAME,
            "description": READ_SOURCE_TOOL_DESCRIPTION,
            "strict": True,
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["event_id"],
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "要翻看原文的记忆 id（如 EVT-001-aaaa）",
                    },
                },
            },
        },
    }]


def validate_writer_arguments(args: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """对 Memory Writer tool call 的 arguments 做严格校验。

    返回 (validated, error)。校验失败的 Event 一律拒绝写入。
    """
    if not isinstance(args, dict):
        return None, "arguments 不是对象"

    unknown = set(args) - {"event", "recall_routes", "quote_spans"}
    if unknown:
        return None, f"多余的顶层字段: {sorted(unknown)}"

    # quote_spans 必填（2026-10-12 裁决：新事件一律带真实时间坐标，
    # 不给/给空都算不合法——粗锚点只作为旧事件的查询时降级存在）
    if "quote_spans" not in args:
        return None, "缺少字段: quote_spans（起尾句对，逐字摘自对话原文）"

    event = args.get("event")
    required_event = [
        "summary", "participants", "objects", "context", "core",
        "outcome", "state", "affect", "significance", "details",
        "key_quotes",
    ]
    if not isinstance(event, dict):
        return None, "event 不是对象"
    missing = [k for k in required_event if k not in event]
    if missing:
        return None, f"event 缺少字段: {missing}"
    extra = set(event) - set(required_event)
    if extra:
        return None, f"event 含未声明字段（LLM 不得提供系统字段）: {sorted(extra)}"
    if not isinstance(event["summary"], str) or not event["summary"].strip():
        return None, "summary 必须是非空字符串"
    if not isinstance(event["core"], str) or not event["core"].strip():
        return None, "core 必须是非空字符串"

    list_fields = ["participants", "objects", "details"]
    for k in list_fields:
        if not isinstance(event[k], list) or not all(isinstance(i, str) for i in event[k]):
            return None, f"event.{k} 必须是字符串数组"
    for k in ["context", "outcome", "state", "affect", "significance"]:
        if not isinstance(event[k], str):
            return None, f"event.{k} 必须是字符串"

    # key_quotes（r4 第 3 项）：数组、元素 speaker∈{user,ai} + 非空 text；
    # 上限 3 条（baseline），超出整条拒绝
    kq = event.get("key_quotes")
    if not isinstance(kq, list):
        return None, "event.key_quotes 必须是数组"
    if len(kq) > 3:
        return None, f"event.key_quotes 最多 3 条，得到 {len(kq)} 条"
    allowed_kq = {"speaker", "text"}
    for i, q in enumerate(kq):
        if not isinstance(q, dict):
            return None, f"event.key_quotes[{i}] 不是对象"
        if set(q) - allowed_kq:
            return None, f"event.key_quotes[{i}] 含未声明字段: {sorted(set(q) - allowed_kq)}"
        if q.get("speaker") not in ("user", "ai"):
            return None, f"event.key_quotes[{i}].speaker 必须是 user 或 ai"
        if not isinstance(q.get("text"), str) or not q["text"].strip():
            return None, f"event.key_quotes[{i}].text 必须是非空字符串"

    routes = args.get("recall_routes")
    if not isinstance(routes, list) or not routes:
        return None, "recall_routes 必须是非空数组"
    allowed_route = {"cue_type", "cue"}
    for i, r in enumerate(routes):
        if not isinstance(r, dict):
            return None, f"recall_routes[{i}] 不是对象"
        if set(r) - allowed_route:
            return None, f"recall_routes[{i}] 含未声明字段: {sorted(set(r) - allowed_route)}"
        if r.get("cue_type") not in ("lexical", "conceptual", "relational", "situational"):
            return None, f"recall_routes[{i}].cue_type 非法"
        if not isinstance(r.get("cue"), str) or not r["cue"].strip():
            return None, f"recall_routes[{i}].cue 必须是非空字符串"

    # quote_spans 必填非空：逐对校验（起/尾句均须非空字符串）
    quote_spans = args.get("quote_spans")
    if not isinstance(quote_spans, list) or not quote_spans:
        return None, "quote_spans 必须是非空数组（至少一对起尾句，逐字摘自对话原文）"
    allowed_q = {"start_quote", "end_quote"}
    for i, q in enumerate(quote_spans):
        if not isinstance(q, dict):
            return None, f"quote_spans[{i}] 不是对象"
        if set(q) - allowed_q:
            return None, f"quote_spans[{i}] 含未声明字段: {sorted(set(q) - allowed_q)}"
        for k in ("start_quote", "end_quote"):
            if not isinstance(q.get(k), str) or not q[k].strip():
                return None, f"quote_spans[{i}].{k} 必须是非空字符串"

    return {"event": event, "recall_routes": routes,
            "quote_spans": quote_spans}, None


# ---------------------------------------------------------------------------
# revise_memory_event（2026-10-14 momo 拍板：冷静期修正）
# ---------------------------------------------------------------------------

REVISE_TOOL_NAME = "revise_memory_event"

# 可修改字段 = 对事实的叙述（含参与者的更正）；cue 经 recall_cues 整体重设。
# 锁死 = 与原文/系统的绑定（id、时间戳、出处、摘录）——摘录与原文的绑定
# 一动双盲核对就废，出处一动回指就断。
REVISE_ALLOWED_FIELDS = ("summary", "participants", "objects", "context",
                         "core", "outcome", "state", "affect",
                         "significance", "details")
REVISE_LOCKED_FIELDS = ("key_quotes", "quote_spans", "turn_refs",
                        "raw_ref", "source_ref")

REVISE_TOOL_DESCRIPTION = (
    "修改一条自己写下的记忆（冷静期内）。只在用户当场指出记忆内容"
    "与事实不符（例如把「去过某地」记成了「住在某地」）时调用："
    "提供 event_id 和需要修正的字段。系统字段（id、时间戳、出处、"
    "逐字摘录）不可修改；已进入夜间维护的记忆不可修改——那种情况"
    "请改用 create_memory_event 写一条更正记忆并说明修正关系。"
)


def build_revise_tool_spec() -> list[dict[str, Any]]:
    """revise_memory_event 的 MCP 工具 schema（旁路专用，代理循环不挂）。"""
    props: dict[str, Any] = {
        "event_id": {"type": "string",
                     "description": "要修改的记忆 id"},
        "recall_cues": {
            "type": "array",
            "description": "（可选）整体重设这条记忆的召回线索"
                           "（原来怎么被想起来的）；提供即全量替换",
            "items": {"type": "string"},
        },
    }
    for f in REVISE_ALLOWED_FIELDS:
        props[f] = {"type": "string",
                    "description": f"（可选）{f} 的新内容"}
    return [{
        "type": "function",
        "function": {
            "name": REVISE_TOOL_NAME,
            "description": REVISE_TOOL_DESCRIPTION,
            "strict": False,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": ["event_id"],
            },
        },
    }]


# ---------- search_memory（2026-10-15 设计夜：主动检索，新拟稿待 momo 过目） ----------

SEARCH_TOOL_NAME = "search_memory"

SEARCH_TOOL_DESCRIPTION = (
    "按内容主动检索记忆库（捞取：自动浮现之外的另一条路）。"
    "想确认某件事有没有发生过、寻找一段记得大概却说不清位置的经历、"
    "或按时间范围回顾时调用。query 用自然语言描述要找的内容，"
    "不要求出现事件原文的字词；time_range 可选，限定发生时间"
    "（支持「昨天/上周/最近3天」等相对表达或 ISO 起止）；"
    "k 可选，返回条数上限（默认 5，最多 20，超限自动钳制并告知）。"
    "返回：实体卡（若命中心智空间）+ 命中条目清单（含状态标注——"
    "淡忘中的/已被修正的会如实标注）+ 命中形态说明。"
    "没有找到就如实说记不清，可换说法再查一次，不要编造。"
)


def build_search_tool_spec() -> list[dict[str, Any]]:
    return [{
        "type": "function",
        "function": {
            "name": SEARCH_TOOL_NAME,
            "description": SEARCH_TOOL_DESCRIPTION,
            "strict": False,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要找的内容，自然语言描述",
                    },
                    "time_range": {
                        "type": "string",
                        "description": ("可选，发生时间过滤：ISO 起止"
                                        "（YYYY-MM-DDTHH:MM:SS/…）或相对表达"
                                        "（昨天/上周/最近N天/近两周）"),
                    },
                    "k": {
                        "type": "integer",
                        "description": "可选，返回条数上限（默认 3，最大 5）——检索是定位器，超出说明 query 太泛",
                    },
                },
                "required": ["query"],
            },
        },
    }]


def parse_tool_call(message: dict[str, Any]) -> dict[str, Any] | None:
    """从 writer 返回的 message 中提取 create_memory_event 调用。

    返回 None 表示 writer 决定不创建 Event。arguments 解析失败抛 LLMError 由上层隔离。
    """
    calls = message.get("tool_calls") or []
    for call in calls:
        fn = (call or {}).get("function") or {}
        if fn.get("name") == TOOL_NAME:
            raw = fn.get("arguments") or "{}"
            args = _loads_lenient(raw) if isinstance(raw, str) else raw
            return args
    return None


def _loads_lenient(raw: str) -> dict[str, Any]:
    """宽容解析 writer 的 tool arguments。

    背景：部分 provider（如 glm-4.7）偶发截断长 JSON arguments。
    策略（baseline，不是已证明设计）：
    1. 直接 loads；
    2. 失败则逐层补全未闭合的 { [ 与悬挂字符串，尝试修复；
    3. 修不了一点截断内容就抛 ValueError，宁可丢弃也不写入残缺 Event。
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    text = raw.strip()
    # 去掉悬挂的引号/逗号尾巴
    while text and text[-1] in ',"':
        text = text[:-1]

    # 若当前处于字符串内部，先补一个结束引号
    in_str = False
    escape = False
    for ch in text:
        if escape:
            escape = False
        elif ch == "\\":
            escape = True
        elif ch == '"':
            in_str = not in_str
    if in_str:
        text += '"'

    stack = []
    for ch in text:
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
    text += "".join("}" if c == "{" else "]" for c in reversed(stack))

    try:
        args = json.loads(text)
    except json.JSONDecodeError as ex:
        raise ValueError(
            f"tool arguments 不是合法 JSON 且无法修复: {ex}; 原始长度 {len(raw)}") from ex
    # 修复产物仍要走完整校验，字段残缺会被 validate_writer_arguments 拒绝
    return args
