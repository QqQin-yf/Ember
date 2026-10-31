# -*- coding: utf-8 -*-
"""记忆工具定义与字段校验；写作说明仅在工具 description 中。"""
from __future__ import annotations

import re
from typing import Any

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
                                "description": (
                                    "这段经历的参与者。这轮对话的双方都在场时"
                                    "双方都算——ai 说过话或被涉及就带上 "
                                    "\"ai\"，不要只写 user"
                                ),
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
                                "description": (
                                    "重要度打分：1-10 的整数 + 冒号 + 一句话"
                                    "理由（如「8：Deliz 开发线的转折」）。"
                                    "10=重大项目转折或确定永远要记住的事；"
                                    "8-9=对进行中事务有实质影响的进展、发现、"
                                    "决定；5-7=有内容但偏背景；1-4=琐碎——"
                                    "1-4 的事不要调用本工具（随口带过的偏好、"
                                    "习惯、单点琐事不写成长期记忆）"
                                ),
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
                            "不输出轮次编号。尽可能保留每段经历的完整对话区间，"
                            "不要为了排除信息量低的回应、过渡或闲话而断开区间。"
                            "只有出现 A → B → A 的事件轮换，即 A 已告一段落、"
                            "转入另一事件 B、之后又重新谈起 A，才将 A 的出处拆成多个区间。"
                            "当两件事交织、难以划开边界时，先判断它们是否其实属于同一件经历："
                            "如果是，合并为一条 Event，保留完整区间；如果确实是两件不同的经历，"
                            "分别写成两条 Event，各自保留完整上下文，出处区间允许重叠，甚至完全相同。"
                            "定位先按原文子串匹配；未命中时忽略 emoji、标点、空白和 Markdown 标记，"
                            "按文字及数字的原顺序连续匹配。容错后仅有一条消息候选才接受；"
                            "若返回 quote_ambiguous，请扩长报错端摘录、加入独特原文文字后重试。"
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


class WriterArgumentError(str):
    """Keep the legacy string API while carrying an exact field and remedy."""
    def __new__(cls, message: str, field: str, hint: str):
        value = super().__new__(cls, message)
        value.field = field
        value.hint = hint
        return value


def _invalid_writer(field: str, message: str, hint: str | None = None):
    hints = {
        "recall_routes": "请提供至少一条召回线索，例如 [{\"cue_type\": \"lexical\", \"cue\": \"关键词\"}]。",
        "quote_spans": "请提供至少一组 start_quote / end_quote，逐字摘自真实原文。",
        "event.significance": "请使用 1-10 的数字加冒号和理由，例如 8：关系的重要转折。",
        "event.key_quotes": "请提供最多三条逐字摘录；无摘录时传 []。",
    }
    if hint is None and field.endswith(".cue"):
        hint = "请填写非空的召回关键词，不能只含空白字符。"
    if hint is None and field.endswith((".start_quote", ".end_quote")):
        hint = "请逐字复制真实原文中的起句或尾句，不能留空。"
    return None, WriterArgumentError(message, field,
        hint or hints.get(field) or f"请按 {field} 的类型和约束修改参数后重新调用。")


def writer_error_result(error: str) -> dict[str, Any]:
    result = {"status": "failed", "code": "invalid_tool_arguments",
              "error": f"validation failed: {error}"}
    if isinstance(error, WriterArgumentError):
        result.update(field=error.field, hint=error.hint)
    return result


def validate_writer_arguments(args: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """对 Memory Writer tool call 的 arguments 做严格校验。

    返回 (validated, error)。校验失败的 Event 一律拒绝写入。
    """
    if not isinstance(args, dict):
        return _invalid_writer('$', "arguments 不是对象")

    event = args.get("event")
    if isinstance(event, dict):
        for field in ("recall_routes", "quote_spans"):
            if field not in args and field in event:
                return _invalid_writer(field,
                    f"{field} 放错层级：当前位于 event.{field}，应放在顶层。",
                    f"将 event.{field} 移出，与 event 平级，保留原内容。")

    unknown = set(args) - {"event", "recall_routes", "quote_spans"}
    if unknown:
        return _invalid_writer('$', f"多余的顶层字段: {sorted(unknown)}")

    # quote_spans 必填（2026-10-12 裁决：新事件一律带真实时间坐标，
    # 不给/给空都算不合法——粗锚点只作为旧事件的查询时降级存在）
    if "quote_spans" not in args:
        return _invalid_writer('quote_spans', "缺少字段: quote_spans（起尾句对，逐字摘自对话原文）")

    event = args.get("event")
    required_event = [
        "summary", "participants", "objects", "context", "core",
        "outcome", "state", "affect", "significance", "details",
        "key_quotes",
    ]
    if not isinstance(event, dict):
        return _invalid_writer('event', "event 不是对象")
    missing = [k for k in required_event if k not in event]
    if missing:
        return _invalid_writer(f'event.{missing[0]}', f"event 缺少字段: {missing}")
    extra = set(event) - set(required_event)
    if extra:
        return _invalid_writer('event', f"event 含未声明字段（LLM 不得提供系统字段）: {sorted(extra)}")
    if not isinstance(event["summary"], str) or not event["summary"].strip():
        return _invalid_writer('event.summary', "summary 必须是非空字符串")
    if not isinstance(event["core"], str) or not event["core"].strip():
        return _invalid_writer('event.core', "core 必须是非空字符串")

    list_fields = ["participants", "objects", "details"]
    for k in list_fields:
        if not isinstance(event[k], list) or not all(isinstance(i, str) for i in event[k]):
            return _invalid_writer(f'event.{k}', f"event.{k} 必须是字符串数组")
    for k in ["context", "outcome", "state", "affect", "significance"]:
        if not isinstance(event[k], str):
            return _invalid_writer(f'event.{k}', f"event.{k} 必须是字符串")
    # significance 数字化（2026-10-17 momo：简报分层依据，格式必须可靠）
    if not re.match(r"\s*(10|[1-9])\s*[：:]", event["significance"]):
        return _invalid_writer('event.significance', "significance 必须以 1-10 的数字开头，后跟冒号和"
                      "一句话理由（如「8：Deliz 开发线的转折」）")

    # key_quotes（r4 第 3 项）：数组、元素 speaker∈{user,ai} + 非空 text；
    # 上限 3 条（baseline），超出整条拒绝
    kq = event.get("key_quotes")
    if not isinstance(kq, list):
        return _invalid_writer('event.key_quotes', "event.key_quotes 必须是数组")
    if len(kq) > 3:
        return _invalid_writer('event.key_quotes', f"event.key_quotes 最多 3 条，得到 {len(kq)} 条")
    allowed_kq = {"speaker", "text"}
    for i, q in enumerate(kq):
        if not isinstance(q, dict):
            return _invalid_writer(f'event.key_quotes[{i}]', f"event.key_quotes[{i}] 不是对象")
        if set(q) - allowed_kq:
            return _invalid_writer(f'event.key_quotes[{i}]', f"event.key_quotes[{i}] 含未声明字段: {sorted(set(q) - allowed_kq)}")
        if q.get("speaker") not in ("user", "ai"):
            return _invalid_writer(f'event.key_quotes[{i}].speaker', f"event.key_quotes[{i}].speaker 必须是 user 或 ai")
        if not isinstance(q.get("text"), str) or not q["text"].strip():
            return _invalid_writer(f'event.key_quotes[{i}].text', f"event.key_quotes[{i}].text 必须是非空字符串")

    routes = args.get("recall_routes")
    if not isinstance(routes, list) or not routes:
        return _invalid_writer('recall_routes', "recall_routes 必须是非空数组")
    allowed_route = {"cue_type", "cue"}
    for i, r in enumerate(routes):
        if not isinstance(r, dict):
            return _invalid_writer(f'recall_routes[{i}]', f"recall_routes[{i}] 不是对象")
        if set(r) - allowed_route:
            return _invalid_writer(f'recall_routes[{i}]', f"recall_routes[{i}] 含未声明字段: {sorted(set(r) - allowed_route)}")
        if r.get("cue_type") not in ("lexical", "conceptual", "relational", "situational"):
            return _invalid_writer(f'recall_routes[{i}].cue_type', f"recall_routes[{i}].cue_type 非法")
        if not isinstance(r.get("cue"), str) or not r["cue"].strip():
            return _invalid_writer(f'recall_routes[{i}].cue', f"recall_routes[{i}].cue 必须是非空字符串")

    # quote_spans 必填非空：逐对校验（起/尾句均须非空字符串）
    quote_spans = args.get("quote_spans")
    if not isinstance(quote_spans, list) or not quote_spans:
        return _invalid_writer('quote_spans', "quote_spans 必须是非空数组（至少一对起尾句，逐字摘自对话原文）")
    allowed_q = {"start_quote", "end_quote"}
    for i, q in enumerate(quote_spans):
        if not isinstance(q, dict):
            return _invalid_writer(f'quote_spans[{i}]', f"quote_spans[{i}] 不是对象")
        if set(q) - allowed_q:
            return _invalid_writer(f'quote_spans[{i}]', f"quote_spans[{i}] 含未声明字段: {sorted(set(q) - allowed_q)}")
        for k in ("start_quote", "end_quote"):
            if not isinstance(q.get(k), str) or not q[k].strip():
                return _invalid_writer(f'quote_spans[{i}].{k}', f"quote_spans[{i}].{k} 必须是非空字符串")

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
    """revise_memory_event 的 MCP 工具 schema（旁路工具）。"""
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


# ---------- pin_memory / unpin_memory（2026-10-17 设计夜：钉住联动） ----------

PIN_TOOL_NAME = "pin_memory"
UNPIN_TOOL_NAME = "unpin_memory"

PIN_TOOL_DESCRIPTION = (
    "把一条记忆钉住：它会常驻每次开场简报（给原文层），并且不再自动"
    "浮现在召回中（已常驻在场，再浮是复读；主动检索不受影响）。"
    "只在用户明确要求「钉住」「记住这事别忘」时调用，取消钉住用 "
    "unpin_memory。"
)

UNPIN_TOOL_DESCRIPTION = (
    "取消一条记忆的钉住。钉住状态可通过 search_memory 的状态标注确认。"
)


def build_pin_tool_specs() -> list[dict[str, Any]]:
    """钉住/取消钉住的 MCP 工具 schema（旁路工具）。"""
    def _spec(name: str, desc: str) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": desc,
                "strict": False,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "event_id": {
                            "type": "string",
                            "description": "要操作的记忆 id",
                        },
                    },
                    "required": ["event_id"],
                },
            },
        }
    return [_spec(PIN_TOOL_NAME, PIN_TOOL_DESCRIPTION),
            _spec(UNPIN_TOOL_NAME, UNPIN_TOOL_DESCRIPTION)]
