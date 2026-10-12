# -*- coding: utf-8 -*-
"""Ember v3 Phase 2 — OpenAI-compatible LLM client（含 Mock Provider）。

- OpenAICompatClient：转发到任意 OpenAI-compatible /v1/chat/completions。
- MockLLMClient：无 Key 测试用。tools 在场时扮演 Memory Writer，否则扮演 Chat。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

USER_AGENT = "EmberProxy/0.2"


class LLMError(Exception):
    """LLM 调用失败（网络/HTTP/响应格式）。"""


class OpenAICompatClient:
    """最小 OpenAI-compatible 客户端，只用标准库。"""

    def __init__(self, base_url: str, api_key: str, timeout: int = 120):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def chat_completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if temperature is not None:
            payload["temperature"] = temperature

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as ex:
            detail = ""
            try:
                detail = ex.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise LLMError(f"provider HTTP {ex.code}: {detail}") from ex
        except (urllib.error.URLError, TimeoutError, OSError) as ex:
            raise LLMError(f"provider 连接失败: {ex}") from ex

        try:
            return body["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as ex:
            raise LLMError(f"provider 响应格式异常: {ex}") from ex


class MockLLMClient:
    """无 Key 测试：模拟 Phase 3 嵌入式架构下的模型行为。

    同一次调用里模型既是对话者又是记忆写入者（momo 22:27 裁决）：
    - 带工具的历史里，最后一条 user 消息含触发词（决定/改成/上线...）
      → 发出 create_memory_event tool call（先写记忆，说完话）。
    - 否则普通回复，不碰工具。
    仅用于本地跑通流程，不构成任何设计结论（铁律）。
    """

    TRIGGERS = ("决定", "定稿", "改成", "完成", "发布", "上线", "采用")

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def chat_completion(self, model, messages, tools=None, **kw):
        self.calls.append({"model": model, "messages": messages, "has_tools": bool(tools)})
        # 已经在 tool 结果回传后的续跑：直接给最终回复
        if messages and messages[-1].get("role") == "tool":
            return {"role": "assistant",
                    "content": "（mock）记忆已写下。我们继续。"}
        text = self._last_user_text(messages)
        if tools and any(t in text for t in self.TRIGGERS):
            return self._tool_call_reply(text)
        return self._chat_reply(text)

    def _last_user_text(self, messages) -> str:
        for m in reversed(messages):
            if m.get("role") == "user":
                c = m.get("content", "")
                return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
        return ""

    def _chat_reply(self, text: str) -> dict[str, Any]:
        return {"role": "assistant",
                "content": f"（mock 回复）收到：{text}\n当前 Provider 是 Mock，配置真实 Key 后这里会是正常回答。"}

    def _tool_call_reply(self, text: str) -> dict[str, Any]:
        if "Ember" in text:
            anchor = "Ember"
            extra_routes = [
                {"cue_type": "conceptual", "cue": "传播模型重新设计"},
                {"cue_type": "lexical", "cue": "Route Search"},
            ]
        else:
            anchor = "测试触发词"
            extra_routes = []
        args = {
            "event": {
                "summary": f"（mock）一段包含明确决定的经历：{text[:40]}",
                "participants": ["momo", "AI"],
                "objects": ["Ember"] if "Ember" in text else ["测试对象"],
                "context": "Mock Provider 本地测试会话",
                "core": "我们讨论了这个问题并一起做出了决定",
                "outcome": "形成了明确结论",
                "state": "进入下一阶段",
                "affect": "平静",
                "significance": "mock 测试用 Event",
                "details": ["本 Event 由 Mock Memory Writer 生成"],
                "key_quotes": [],
            },
            "recall_routes": [{"cue_type": "lexical", "cue": anchor}] + extra_routes,
            # quote_spans 必填（2026-10-12 裁决）：摘本轮用户原话的首尾片段
            "quote_spans": [{"start_quote": text[:8], "end_quote": text[-8:]}],
        }
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_mock_001",
                "type": "function",
                "function": {
                    "name": "create_memory_event",
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }],
        }
