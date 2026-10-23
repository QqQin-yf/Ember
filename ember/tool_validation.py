"""Actionable MCP input errors without changing the advertised schema."""
from __future__ import annotations

from jsonschema import Draft202012Validator


def validate_tool_input(arguments: dict, schema: dict) -> dict | None:
    # Diagnose misplaced root fields before the generic required-property error.
    event = arguments.get("event")
    if isinstance(event, dict):
        for field in schema.get("required", []):
            if field not in arguments and field in event:
                return {
                    "status": "failed", "code": "invalid_tool_arguments",
                    "field": field, "actual_path": f"event.{field}",
                    "error": f"{field} 放错层级：当前位于 event.{field}，顶层缺少 {field}。请将它从 event 内移出，与 event、quote_spans 平级，保留原内容。",
                }
    error = next(Draft202012Validator(schema).iter_errors(arguments), None)
    if error is None:
        return None
    path = ""
    for part in error.absolute_path:
        path += f"[{part}]" if isinstance(part, int) else ("." if path else "") + part
    if error.validator == "required":
        missing = next(k for k in error.validator_value if k not in error.instance)
        path = f"{path}.{missing}" if path else missing
        detail = "缺少必填字段，请在此路径补充该字段"
    elif error.validator == "type":
        expected = error.validator_value
        actual = ("null" if error.instance is None else
                  "boolean" if isinstance(error.instance, bool) else
                  "object" if isinstance(error.instance, dict) else
                  "array" if isinstance(error.instance, list) else
                  "string" if isinstance(error.instance, str) else "number")
        detail = f"类型错误：应为 {expected}，实际为 {actual}"
    elif error.validator == "enum":
        detail = f"取值不合法，允许的值为 {error.validator_value}"
    elif error.validator == "additionalProperties":
        extra = sorted(set(error.instance) - set(error.schema.get("properties", {})))
        detail = f"含未声明字段：{extra}，请移除这些字段或核对其层级"
    else:
        detail = f"不符合约束 {error.validator}，请核对工具参数定义"
    return {"status": "failed", "code": "invalid_tool_arguments",
            "field": path or "$", "error": f"{path or '$'}：{detail}"}
