# -*- coding: utf-8 -*-
"""Ember 旁路服务（MCP + hook 形态）——2026-10-12 拍板的接入形态。

与 proxy.py（OpenAI 代理形态，8766）并存运行；前端切换（B1）完成后
proxy 退役。本进程不再位于对话主路径：

- POST /ember/hooks/turn_start   轮前：开轮 + 本地召回，返回注入文本
- POST /ember/hooks/turn_end     轮后：原文存档 + 收轮
- POST /mcp                      MCP Streamable HTTP：create_memory_event /
                                 read_memory_source（schema 与代理形态同源，
                                 见 ember/writer.py build_tools_spec）

降级哲学（与前端 heart 钩子同款）：任何一环失败都不影响主对话——
召回无命中返回 injected_text=null；工具失败回 failed 回执，前端照常
续跑。前端只对可见结果负责注入与展示。

命名纪律（10-11 无感化原则的延续）：MCP server 名与工具名不带 Ember
品牌——工具名会原样出现在模型的工具清单里，机制标识不得成为身份线索。

依赖：pip install "mcp>=1.8"（MCP 官方 Python SDK，自带 starlette/uvicorn）。
鉴权：设置 EMBER_TOKEN 后所有端点要求 Authorization: Bearer <token>。
配置：EMBER_HOST（默认 127.0.0.1）、EMBER_SIDECAR_PORT（默认 8767）、
EMBER_DATA_DIR（与 proxy 共用同一记忆库）。
"""
from __future__ import annotations

import contextlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# .env 加载必须在其他 ember.* import 之前——source_client 等 ember
# 模块的模块级常量（EMBER_SOURCE_URL 等）在 import 时求值
from ember.env import load_env
load_env(os.path.dirname(os.path.abspath(__file__)))

from ember.llm import MockLLMClient
from ember.service import EmberService
from ember.store import EmberStore
from ember.writer import (READ_SOURCE_TOOL_NAME, REVISE_TOOL_NAME,
                          SEARCH_TOOL_NAME, TOOL_NAME)

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
import uvicorn

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("EMBER_DATA_DIR", os.path.join(HERE, "data"))
HOST = os.environ.get("EMBER_HOST", "127.0.0.1")
PORT = int(os.environ.get("EMBER_SIDECAR_PORT", "8767"))
TOKEN = os.environ.get("EMBER_TOKEN", "")

# 旁路形态不调用任何 LLM：召回是本地毫秒级、写入是确定性校验+落盘。
# EmberService 构造器要求的 client 传 Mock——永远轮不到它说话。
SERVICE = EmberService(EmberStore(DATA_DIR), MockLLMClient(), model="sidecar")

# ---------- MCP 层：协议翻译，不掺业务 ----------

mcp_server = Server("memory")


@mcp_server.list_tools()
async def _list_tools() -> list[types.Tool]:
    out: list[types.Tool] = []
    for spec in SERVICE.sidecar_tool_specs():
        fn = spec["function"]
        out.append(types.Tool(name=fn["name"], description=fn["description"],
                              inputSchema=fn["parameters"]))
    return out


@mcp_server.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == TOOL_NAME:
        result = SERVICE.sidecar_create_memory_event(arguments or {})
    elif name == REVISE_TOOL_NAME:
        result = SERVICE.sidecar_revise_memory_event(arguments or {})
    elif name == READ_SOURCE_TOOL_NAME:
        result = SERVICE.sidecar_read_memory_source(
            (arguments or {}).get("event_id") or "")
    elif name == SEARCH_TOOL_NAME:
        result = SERVICE.sidecar_search_memory(arguments or {})
    else:
        result = {"status": "failed", "error": f"未知工具: {name}"}
    return [types.TextContent(type="text",
                              text=json.dumps(result, ensure_ascii=False))]


session_manager = StreamableHTTPSessionManager(
    app=mcp_server, json_response=False, stateless=True)


async def _handle_mcp(scope, receive, send) -> None:
    await session_manager.handle_request(scope, receive, send)


@contextlib.asynccontextmanager
async def _lifespan(app):
    async with session_manager.run():
        yield


# ---------- 钩子层：失败如实上报，绝不抛 500 挡前端 ----------

def _opt_int(body: dict, key: str) -> int | None:
    """钩子请求体的可选整数 id（message_id 类）：int 或数字串 → int，
    其余（缺省/null/空/非数字）→ None。脏值不挡主流程。"""
    v = body.get(key)
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


async def _hook_turn_start(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = str(body.get("message") or "")
    if not message.strip():
        return JSONResponse({"injected": False, "injected_text": None,
                             "reason": "empty message"})
    return JSONResponse(SERVICE.hook_turn_start(
        body.get("session_id"), message, body.get("heart_turn_id"),
        user_message_id=_opt_int(body, "user_message_id"),
        context_start_id=_opt_int(body, "context_start_id")))


async def _hook_turn_end(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_turn = body.get("turn")
    try:
        turn = int(raw_turn) if raw_turn is not None else None
    except (TypeError, ValueError):
        turn = None
    return JSONResponse(SERVICE.hook_turn_end(
        body.get("session_id"), str(body.get("assistant_reply") or ""),
        turn, assistant_message_id=_opt_int(body, "assistant_message_id")))


# ---------- 挂关系（2026-10-14 设计稿 v2：借窗口跑，app 编排） ----------

async def _sleep_link_batches(request: Request) -> JSONResponse:
    """接口 1：材料出队——app 贴进窗口末尾给正主 AI 判联。"""
    return JSONResponse(SERVICE.sidecar_link_batches())


async def _sleep_link_submit(request: Request) -> JSONResponse:
    """接口 2：交卷——app 转交窗口 AI 的 link_relations 结果。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    return JSONResponse(SERVICE.sidecar_link_submit(body))


async def _sleep_link_progress(request: Request) -> JSONResponse:
    """接口 3：进度——断线恢复时 app 问跑到哪了。"""
    return JSONResponse(SERVICE.sidecar_link_progress())


async def _sleep_structure_run(request: Request) -> JSONResponse:
    """结构维护：散事件归属（第三方 LLM）+ 旧 cue 收紧。

    编排顺序：挂在 link/submit 清完之后——挂完关系不等于没活儿，
    还有这一步（契约见 API.md「编排顺序」）。
    """
    return JSONResponse(SERVICE.run_structure_maintenance())


async def _sleep_sediment_evidence(request: Request) -> JSONResponse:
    """沉淀证据接收：app 存下一版新沉淀后，交版本号 + 证据 id 挂账。

    只做追加——证据为空 app 不调本接口（契约见 API.md §6）。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    return JSONResponse(SERVICE.sidecar_sediment_evidence(body))


class BearerAuthMiddleware:
    """最小 Bearer 鉴权（EMBER_TOKEN 设置时生效），MCP 与钩子一视同仁。"""

    def __init__(self, app):
        self.app = app
        self._expect = f"Bearer {TOKEN}".encode("ascii")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and TOKEN:
            headers = {k.lower(): v for k, v in scope.get("headers") or []}
            if headers.get(b"authorization") != self._expect:
                await JSONResponse({"error": "unauthorized"},
                                   status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


app = BearerAuthMiddleware(Starlette(
    routes=[
        Route("/ember/hooks/turn_start", _hook_turn_start, methods=["POST"]),
        Route("/ember/hooks/turn_end", _hook_turn_end, methods=["POST"]),
        Route("/ember/sleep/link/batches", _sleep_link_batches,
              methods=["GET"]),
        Route("/ember/sleep/link/submit", _sleep_link_submit,
              methods=["POST"]),
        Route("/ember/sleep/link/progress", _sleep_link_progress,
              methods=["GET"]),
        Route("/ember/sleep/structure/run", _sleep_structure_run,
              methods=["POST"]),
        Route("/ember/sleep/sediment/evidence", _sleep_sediment_evidence,
              methods=["POST"]),
        Mount("/mcp", app=_handle_mcp),
    ],
    lifespan=_lifespan,
))


def main() -> None:
    print(f"[ember-sidecar] listening on http://{HOST}:{PORT}")
    print(f"[ember-sidecar] mcp:   http://{HOST}:{PORT}/mcp")
    print("[ember-sidecar] hooks: POST /ember/hooks/turn_start | /ember/hooks/turn_end")
    print(f"[ember-sidecar] store: {DATA_DIR}")
    print(f"[ember-sidecar] auth:  {'Bearer EMBER_TOKEN' if TOKEN else '未启用（本机自用）'}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
