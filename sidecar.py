# -*- coding: utf-8 -*-
"""Ember 旁路服务（MCP + hook 形态）——2026-10-12 拍板的接入形态。

与 proxy.py（OpenAI 代理形态，8766）并存运行；前端切换（B1）完成后
proxy 退役。本进程不再位于对话主路径：

- POST /ember/hooks/turn_start   轮前：开轮 + 本地召回，返回注入文本
- POST /ember/hooks/turn_end     轮后：原文存档 + 收轮
- POST /ember/briefing/refresh · GET /ember/briefing   简报（截断刷新/取用）
- POST /ember/memory/pin · /ember/memory/unpin         钉住/取消（幂等）
- POST /mcp                      MCP Streamable HTTP：create_memory_event /
                                 read_memory_source（schema 与代理形态同源，
                                 见 ember/writer.py build_tools_spec）

多实例（2026-10-17，一颗心一套记忆库）：本进程持实例注册表
（ember/instances.py），每个实例独立的 EmberStore + EmberService，
记忆库目录挂在对应 heart 实例的数据目录下。路由与 heart 同款：
请求头 X-Heart-Instance（或 ?inst= 查询参数），未带 = "default"，
清单外实例 404。单实例部署完全无感——不带路由头的所有请求都落在
default 上。

降级哲学（与前端 heart 钩子同款）：任何一环失败都不影响主对话——
召回无命中返回 injected_text=null；工具失败回 failed 回执，前端照常
续跑。前端只对可见结果负责注入与展示。

命名纪律（10-11 无感化原则的延续）：MCP server 名与工具名不带 Ember
品牌——工具名会原样出现在模型的工具清单里，机制标识不得成为身份线索。

依赖：pip install "mcp>=1.8"（MCP 官方 Python SDK，自带 starlette/uvicorn）。
鉴权：设置 EMBER_TOKEN 后所有端点要求 Authorization: Bearer <token>。
配置：EMBER_DATA_ROOT（heart 的 data/ 根目录，记忆库挂在各实例目录下，
     详见 ember/instances.py）、EMBER_INSTANCES（实例清单，缺省
     "default"）、EMBER_HOST（默认 127.0.0.1）、EMBER_SIDECAR_PORT
     （默认 8767）。
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import sys
from urllib.parse import parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# .env 加载必须在其他 ember.* import 之前——source_client 等 ember
# 模块的模块级常量（EMBER_SOURCE_URL 等）在 import 时求值
from ember.env import load_env
load_env(os.path.dirname(os.path.abspath(__file__)))

from ember.instances import InstanceRegistry, parse_instances, resolve_root
from ember.writer import (READ_SOURCE_TOOL_NAME, REVISE_TOOL_NAME,
                          SEARCH_TOOL_NAME, TOOL_NAME,
                          build_pin_tool_specs, build_revise_tool_spec,
                          build_search_tool_spec, build_tools_spec)

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
import uvicorn

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("EMBER_HOST", "127.0.0.1")
PORT = int(os.environ.get("EMBER_SIDECAR_PORT", "8767"))
TOKEN = os.environ.get("EMBER_TOKEN", "")

# 实例注册表：清单外的名字一律 404（宁拒启不裸奔——实例名非法当场炸出）
try:
    REGISTRY = InstanceRegistry(
        resolve_root(),
        parse_instances(os.environ.get("EMBER_INSTANCES", "default")),
    )
except ValueError as exc:
    raise SystemExit(f"[ember-sidecar] 拒绝启动：{exc}")

# MCP 层的实例上下文：HTTP 层解析实例名 → set → 工具执行体取用
# （MCP SDK 的工具回调拿不到 HTTP 请求，用 contextvar 穿协议层）
CURRENT_SERVICE: contextvars.ContextVar = contextvars.ContextVar(
    "ember_current_service", default=None)

# ---------- 实例路由（与 heart 同款：头优先，?inst= 兜底，缺省 default）


def _instance_from_scope(scope) -> str:
    """原始 ASGI scope 版实例名解析（MCP 路径用，拿不到 Request）。"""

    headers = {k.decode("latin-1").lower(): v.decode("latin-1")
               for k, v in scope.get("headers") or []}
    name = (headers.get("x-heart-instance") or "").strip()
    if not name:
        qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
        name = (qs.get("inst") or [""])[0].strip()
    return name or "default"


def _instance_name(request: Request) -> str:
    name = (request.headers.get("x-heart-instance") or "").strip()
    if not name:
        name = (request.query_params.get("inst") or "").strip()
    return name or "default"


def _service_or_404(request: Request):
    """当前请求应落到的实例 service；清单外返回 404 响应（由调用方直发）。"""

    svc = REGISTRY.service(_instance_name(request))
    if svc is None:
        return JSONResponse({"error": "unknown instance"}, status_code=404)
    return svc


# ---------- MCP 层：协议翻译，不掺业务 ----------

mcp_server = Server("memory")


@mcp_server.list_tools()
async def _list_tools() -> list[types.Tool]:
    # 工具清单全实例同源（纯函数构建，不触任何实例的数据）；
    # 组成必须与 EmberService.sidecar_tool_specs 保持一致
    specs = (build_tools_spec() + [build_revise_tool_spec()[0]]
             + build_search_tool_spec() + build_pin_tool_specs())
    out: list[types.Tool] = []
    for spec in specs:
        fn = spec["function"]
        out.append(types.Tool(name=fn["name"], description=fn["description"],
                              inputSchema=fn["parameters"]))
    return out


@mcp_server.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    svc = CURRENT_SERVICE.get()
    if svc is None:
        result = {"status": "failed", "error": "实例上下文丢失（无进行中的路由）"}
    elif name == TOOL_NAME:
        result = svc.sidecar_create_memory_event(arguments or {})
    elif name == REVISE_TOOL_NAME:
        result = svc.sidecar_revise_memory_event(arguments or {})
    elif name == READ_SOURCE_TOOL_NAME:
        result = svc.sidecar_read_memory_source(
            (arguments or {}).get("event_id") or "")
    elif name == SEARCH_TOOL_NAME:
        result = svc.sidecar_search_memory(arguments or {})
    else:
        result = {"status": "failed", "error": f"未知工具: {name}"}
    return [types.TextContent(type="text",
                              text=json.dumps(result, ensure_ascii=False))]


session_manager = StreamableHTTPSessionManager(
    app=mcp_server, json_response=False, stateless=True)


async def _handle_mcp(scope, receive, send) -> None:
    svc = REGISTRY.service(_instance_from_scope(scope))
    if svc is None:
        await JSONResponse({"error": "unknown instance"},
                           status_code=404)(scope, receive, send)
        return
    token = CURRENT_SERVICE.set(svc)
    try:
        await session_manager.handle_request(scope, receive, send)
    finally:
        CURRENT_SERVICE.reset(token)


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
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    try:
        body = await request.json()
    except Exception:
        body = {}
    message = str(body.get("message") or "")
    if not message.strip():
        return JSONResponse({"injected": False, "injected_text": None,
                             "reason": "empty message"})
    return JSONResponse(svc.hook_turn_start(
        body.get("session_id"), message, body.get("heart_turn_id"),
        user_message_id=_opt_int(body, "user_message_id"),
        context_start_id=_opt_int(body, "context_start_id")))


async def _hook_turn_end(request: Request) -> JSONResponse:
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_turn = body.get("turn")
    try:
        turn = int(raw_turn) if raw_turn is not None else None
    except (TypeError, ValueError):
        turn = None
    return JSONResponse(svc.hook_turn_end(
        body.get("session_id"), str(body.get("assistant_reply") or ""),
        turn, assistant_message_id=_opt_int(body, "assistant_message_id")))


# ---------- 挂关系（2026-10-14 设计稿 v2：借窗口跑，app 编排） ----------

async def _sleep_link_batches(request: Request) -> JSONResponse:
    """接口 1：材料出队——app 贴进窗口末尾给正主 AI 判联。"""
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    return JSONResponse(svc.sidecar_link_batches())


async def _sleep_link_submit(request: Request) -> JSONResponse:
    """接口 2：交卷——app 转交窗口 AI 的 link_relations 结果。"""
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    try:
        body = await request.json()
    except Exception:
        body = {}
    return JSONResponse(svc.sidecar_link_submit(body))


async def _sleep_link_progress(request: Request) -> JSONResponse:
    """接口 3：进度——断线恢复时 app 问跑到哪了。"""
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    return JSONResponse(svc.sidecar_link_progress())


async def _sleep_structure_run(request: Request) -> JSONResponse:
    """结构维护：散事件归属（第三方 LLM）+ 旧 cue 收紧。

    编排顺序：挂在 link/submit 清完之后——挂完关系不等于没活儿，
    还有这一步（契约见 API.md「编排顺序」）。
    """
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    return JSONResponse(svc.run_structure_maintenance())


async def _sleep_sediment_evidence(request: Request) -> JSONResponse:
    """沉淀证据接收：app 存下一版新沉淀后，交版本号 + 证据 id 挂账。

    只做追加——证据为空 app 不调本接口（契约见 API.md §6）。
    """
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    try:
        body = await request.json()
    except Exception:
        body = {}
    return JSONResponse(svc.sidecar_sediment_evidence(body))


# ---------- 简报与钉住（2026-10-17 设计夜；HTTP 接线随本次补齐） ----------

async def _briefing_refresh(request: Request) -> JSONResponse:
    """截断时刷新简报（app 编排：丢头留尾截断的同步动作）。"""
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    try:
        body = await request.json()
    except Exception:
        body = {}
    return JSONResponse(svc.sidecar_briefing_refresh(body))


async def _briefing_get(request: Request) -> JSONResponse:
    """取当前简报（断线重进/新会话开场注入）。"""
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    return JSONResponse(svc.sidecar_briefing_get())


async def _pin_memory(request: Request) -> JSONResponse:
    """钉住（幂等；与 pin_memory 工具共用同一执行体）。"""
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    try:
        body = await request.json()
    except Exception:
        body = {}
    return JSONResponse(svc.sidecar_pin_memory(body))


async def _unpin_memory(request: Request) -> JSONResponse:
    """取消钉住（幂等）。"""
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    try:
        body = await request.json()
    except Exception:
        body = {}
    return JSONResponse(svc.sidecar_unpin_memory(body))


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
        Route("/ember/briefing/refresh", _briefing_refresh,
              methods=["POST"]),
        Route("/ember/briefing", _briefing_get, methods=["GET"]),
        Route("/ember/memory/pin", _pin_memory, methods=["POST"]),
        Route("/ember/memory/unpin", _unpin_memory, methods=["POST"]),
        Mount("/mcp", app=_handle_mcp),
    ],
    lifespan=_lifespan,
))


def main() -> None:
    # token 兜底（Claude 2026-10-16）：监听非回环地址又没有 token = 敞门，
    # 宁可拒绝启动也不悄悄裸奔——变量名拼错会当场炸出来
    if HOST not in ("127.0.0.1", "localhost", "::1") and not TOKEN:
        raise SystemExit(
            "[ember-sidecar] 拒绝启动：EMBER_HOST 监听的是非回环地址"
            f"（{HOST}）但 EMBER_TOKEN 为空——跨机部署必须设 token，"
            "否则记忆库对网络不设防。本机自用请显式设 EMBER_HOST=127.0.0.1。")
    print(f"[ember-sidecar] listening on http://{HOST}:{PORT}")
    print(f"[ember-sidecar] mcp:   http://{HOST}:{PORT}/mcp")
    print("[ember-sidecar] hooks: POST /ember/hooks/turn_start | /ember/hooks/turn_end")
    print(f"[ember-sidecar] root: {REGISTRY.root}")
    for name in REGISTRY.names:
        print(f"[ember-sidecar] store[{name}]: {REGISTRY.dir_of(name)}")
    print(f"[ember-sidecar] auth:  {'Bearer EMBER_TOKEN' if TOKEN else '未启用（本机自用）'}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    main()
