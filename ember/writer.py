# -*- coding: utf-8 -*-
"""记忆工具定义与字段校验；写作说明仅在工具 description 中。"""
from __future__ import annotations

import re
from typing import Any

TOOL_NAME = "write_event"
PROMISE_TOOL_NAME = "write_promise"
READ_SOURCE_TOOL_NAME = "read_memory_source"


def build_tools_spec():
    from copy import deepcopy
    event = {"type":"object", "additionalProperties":False,
        "required":["summary","core","significance","kind","focus"],
        "properties":{
            "summary":{"type":"string","minLength":1,"description":"一句话客观、简短地说清这段经历。"},
            "core":{"type":"string","minLength":1,"description":"第一人称的连贯叙述，写给自己的诚实版本：情况、细节、理解和感受都写在这里。"},
            "significance":{"type":"integer","minimum":1,"maximum":10,"description":"重要度整数 1-10。10=重大项目转折或确定永远要记住；8-9=有实质影响的进展、发现、决定；5-7=有内容的背景；1-4=琐碎。"},
            "kind":{"enum":["daily","relationship","project"],"description":"事件主类：日常、关于两人关系本身的互动、项目。"},
            "focus":{"type":"string","minLength":1,"description":"从来源区间逐字摘一句最重要的原话，写法同 start_quote；系统定位对应消息供 pin 截断，不要填写消息编号。"}}}
    routes={"type":"array","minItems":1,"items":{"type":"object","additionalProperties":False,
        "required":["cue_type","cue"],"properties":{
            "cue_type":{"enum":["lexical","conceptual","relational","situational"]},
            "cue":{"type":"string","minLength":1,"description":"未来能唤起这次经历的最小独立线索；专名保持整体。"}}}}
    spans={"type":"array","minItems":1,"items":{"type":"object","additionalProperties":False,
        "required":["start_quote","end_quote"],"properties":{
            "start_quote":{"type":"string","minLength":1},"end_quote":{"type":"string","minLength":1}}},
        "description":"逐字摘录起尾句，保留完整事件区间；只有事件轮换才拆段。"}
    props={"event":event,"recall_routes":routes,"quote_spans":spans,
        "source_slice_ids":{"type":"array","minItems":1,"uniqueItems":True,"items":{"type":"string","minLength":1},"description":"从提取材料写入时提供切片 ID；与 quote_spans 二选一。"}}
    schema={"type":"object","additionalProperties":False,"required":["event","recall_routes"],"properties":props,
        "oneOf":[{"required":["quote_spans"],"not":{"required":["source_slice_ids"]}},
                 {"required":["source_slice_ids"],"not":{"required":["quote_spans"]}}]}
    def spec(name,description,parameters):
        return {"type":"function","function":{"name":name,"description":description,"parameters":parameters}}
    promise=deepcopy(schema)
    pe=promise['properties']['event']; pe['properties']['kind']={"const":"promise"}
    pe['properties']['due']={"type":["string","null"],"description":"具体 ISO 日期时间；无明确时间则 null。"}
    # Status and log are maintained by Ember, never supplied on initial write.
    return [spec(TOOL_NAME,"答应了、说好了、约好了的事，用 write_promise。记录日常、关系或项目经历；系统生成 ID 和时间。来源二选一，成功后不要重复写。",schema),
            spec(PROMISE_TOOL_NAME,"日常、关系、项目，用 write_event。记录单方面承诺或双方约定；kind 固定为 promise，状态初始 open，log 由处理工具追加。",promise),
            spec(READ_SOURCE_TOOL_NAME,"读取记忆对应的原文，核对具体说法或细节。",{"type":"object","additionalProperties":False,"required":["event_id"],"properties":{"event_id":{"type":"string"}}})]


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
        "event.significance": "请使用 1-10 的整数，不带理由。",

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


def validate_writer_arguments(args, *, promise=None):
    from .tool_validation import validate_tool_input
    if not isinstance(args,dict):
        return _invalid_writer('$', 'arguments 不是对象')
    if promise is None:
        promise = (args.get('event') or {}).get('kind') == 'promise'
    spec=build_tools_spec()[1 if promise else 0]['function']['parameters']
    error=validate_tool_input(args,spec)
    if error:
        return _invalid_writer(error['field'],error['error'],error['hint'])
    event=args['event']
    for key in ('summary','core','focus'):
        if not event[key].strip():
            return _invalid_writer('event.'+key, key+' 不能为空')
    if type(event['significance']) is not int:
        return _invalid_writer('event.significance','significance 必须是 1-10 的整数')
    if event.get('due') is not None:
        from datetime import datetime
        try:
            dt=datetime.fromisoformat(event['due'].replace('Z','+00:00'))
            if dt.tzinfo is None:
                raise ValueError()
        except (ValueError,TypeError):
            return _invalid_writer('event.due','due 必须是带时区的 ISO 日期时间或 null')
    for i,r in enumerate(args['recall_routes']):
        if not r['cue'].strip():
            return _invalid_writer(f'recall_routes[{i}].cue','cue 不能为空')
    for i,q in enumerate(args.get('quote_spans',[])):
        for key in ('start_quote','end_quote'):
            if not q[key].strip():
                return _invalid_writer(f'quote_spans[{i}].{key}',key+' 不能为空')
    return args,None


# ---------------------------------------------------------------------------
# revise_memory_event（2026-10-14 momo 拍板：冷静期修正）
# ---------------------------------------------------------------------------

REVISE_TOOL_NAME = "revise_memory_event"

# 可修改字段 = 对事实的叙述（含参与者的更正）；cue 经 recall_cues 整体重设。
# 锁死 = 与原文/系统的绑定（id、时间戳、出处、摘录）——摘录与原文的绑定
# 一动双盲核对就废，出处一动回指就断。
REVISE_ALLOWED_FIELDS = ("summary", "core", "significance", "kind")
REVISE_LOCKED_FIELDS = ("focus", "quote_spans", "turn_refs", "raw_ref", "source_ref", "due", "status", "log")
REVISE_TOOL_DESCRIPTION = "直接修订记忆的叙述或召回线索；约定状态和改期用约定处理工具。原文和系统绑定不可修改。"


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
        props[f] = {"type": "integer" if f == "significance" else "string",
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
