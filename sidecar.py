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

import asyncio
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
from ember.tool_validation import validate_tool_input
from ember.slice_pipeline import SlicePipeline, IngestionJobs, complete_turn, complete_run
from ember.slice_recall import SliceRecall
from ember.slice_maintenance import SliceMaintenanceWorker
from ember.pool_maintenance import PoolMaintenanceWorker, KnowledgeEmbeddingWorker
from ember.memory_maintenance import MemoryMaintenanceWorker
from ember.knowledge_tools import tool_specs as knowledge_tool_specs
from ember.writer import (PIN_TOOL_NAME, UNPIN_TOOL_NAME,
                          READ_SOURCE_TOOL_NAME, REVISE_TOOL_NAME,
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
CURRENT_CALL_CONTEXT: contextvars.ContextVar = contextvars.ContextVar(
    "ember_current_call_context", default=None)

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
    from ember.timeline import tool_specs as timeline_specs
    from ember.organization_tools import tool_specs as organization_specs
    from ember.promises import tool_specs as promise_specs
    specs = (build_tools_spec() + [build_revise_tool_spec()[0]]
             + build_search_tool_spec() + build_pin_tool_specs() + knowledge_tool_specs() + promise_specs() + organization_specs() + timeline_specs())
    out: list[types.Tool] = []
    for spec in specs:
        fn = spec["function"]
        out.append(types.Tool(name=fn["name"], description=fn["description"],
                              inputSchema=fn["parameters"]))
    return out


@mcp_server.call_tool(validate_input=False)
async def _call_tool(name: str, arguments: dict) -> types.CallToolResult:
    # Keep schema enforcement here so SDK errors do not hide the field path/hint.
    tool = next((tool for tool in await _list_tools() if tool.name == name), None)
    if tool is not None:
        error = validate_tool_input(arguments, tool.inputSchema)
        if error is not None:
            return types.CallToolResult(isError=True, content=[types.TextContent(
                type="text", text=json.dumps(error, ensure_ascii=False))])
    svc = CURRENT_SERVICE.get()
    if svc is None:
        result = {"status": "failed", "error": "实例上下文丢失（无进行中的路由）"}
    else:
        result = await asyncio.to_thread(svc.sidecar_call_tool, name, arguments or {},
                                         **(CURRENT_CALL_CONTEXT.get() or {}))
    # Explicit failures must reach the model as tool errors; pending/read states
    # retain their existing semantics and receipt payloads.
    return types.CallToolResult(
        isError=result.get("status") in {"failed", "rejected"} or result.get("ok") is False,
        content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))])


session_manager = StreamableHTTPSessionManager(
    app=mcp_server, json_response=False, stateless=True)


async def _handle_mcp(scope, receive, send) -> None:
    names = {b"x-ember-recall-id": "recall_id", b"x-ember-run-id": "run_id",
             b"x-ember-tool-call-id": "tool_call_id"}
    context = {}
    seen = set()
    for key, value in scope.get("headers") or []:
        key = key.lower()
        if key in names or key == b"x-heart-instance":
            if key in seen:
                await JSONResponse({"error": "Duplicate identity header", "code": "invalid_identity"},
                                   status_code=400)(scope, receive, send)
                return
            seen.add(key)
        if key in names:
            context[names[key]] = value.decode("latin-1")
    svc = REGISTRY.service(_instance_from_scope(scope))
    if svc is None:
        await JSONResponse({"error": "unknown instance"},
                           status_code=404)(scope, receive, send)
        return
    token = CURRENT_SERVICE.set(svc)
    context_token = CURRENT_CALL_CONTEXT.set(context)
    try:
        await session_manager.handle_request(scope, receive, send)
    finally:
        CURRENT_CALL_CONTEXT.reset(context_token)
        CURRENT_SERVICE.reset(token)


async def _expire_recall_snapshots() -> None:
    while True:
        try:
            await asyncio.to_thread(REGISTRY.expire_recall_snapshots)
        except Exception:
            print("[ember-sidecar] recall snapshot cleanup deferred", file=sys.stderr)
        await asyncio.sleep(60)


@contextlib.asynccontextmanager
async def _lifespan(app):
    async with session_manager.run():
        cleanup = asyncio.create_task(_expire_recall_snapshots())
        pipeline = SlicePipeline(REGISTRY)
        app.state.slice_pipeline = pipeline
        # Raw ingestion is pulled after committed Event batches or briefing refresh.
        from ember.source_sync import SourceSyncWorker
        maintenance = SourceSyncWorker(REGISTRY)
        app.state.slice_maintenance = maintenance
        maintaining = asyncio.create_task(maintenance.run())
        pool_worker = PoolMaintenanceWorker(REGISTRY)
        knowledge_worker = KnowledgeEmbeddingWorker(REGISTRY)
        app.state.pool_maintenance = pool_worker
        app.state.knowledge_embedding = knowledge_worker
        # Per-query GLM pool classification is retired.
        embedding_knowledge = asyncio.create_task(knowledge_worker.run())
        from ember.organization import OrganizationWorker
        memory_worker = OrganizationWorker(REGISTRY)
        app.state.memory_maintenance = memory_worker
        memory_maintenance = asyncio.create_task(memory_worker.run())
        from ember.timeline import TimelineWorker
        timeline_worker=TimelineWorker(REGISTRY)
        timeline_task=asyncio.create_task(timeline_worker.run())
        try:
            yield
        finally:
            pipeline.stop_event.set()
            maintenance.stop_event.set()
            pool_worker.stop_event.set()
            knowledge_worker.stop_event.set()
            memory_worker.stop_event.set()
            timeline_worker.stop_event.set()
            await asyncio.gather(maintaining, embedding_knowledge, memory_maintenance, timeline_task)
            cleanup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup


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


def _generation_response(result: dict) -> JSONResponse:
    code = result.get("code")
    if code in ("recall_in_progress", "recall_recovery_pending"):
        status = 202
    elif code == "response_expired":
        status = 410
    elif not code:
        status = 200
    elif code in ("unknown_recall", "unknown_tool_call"):
        status = 404
    elif code in ("invalid_identity", "invalid_status", "invalid_arguments"):
        status = 400
    else:
        status = 409
    headers = {"Retry-After": str(result.get("retry_after_sec", 2))} if status == 202 else None
    return JSONResponse(result, status_code=status, headers=headers)


def _run_response(result: dict) -> JSONResponse:
    code = result.get("code")
    status = (404 if code == "run_not_found" else
              503 if code == "run_source_unavailable" else
              400 if code in {"invalid_arguments", "invalid_identity"} else
              409 if code else 200)
    return JSONResponse(result, status_code=status,
                        headers={"Retry-After": "2"} if status == 503 else None)


async def _run_lifecycle(request: Request) -> JSONResponse:
    if sum(k.lower() == b"x-heart-instance" for k, _ in request.scope.get("headers", [])) > 1:
        return _run_response({"status": "failed", "code": "invalid_identity",
                              "error": "Duplicate instance header"})
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    if request.method == "GET":
        return _run_response(await asyncio.to_thread(svc.sidecar_run_status, request.path_params["run_id"]))
    try:
        body = await request.json()
    except Exception:
        return _run_response({"status": "failed", "code": "invalid_arguments",
                              "field": "$", "error": "Expected JSON object"})
    if request.url.path.endswith("/start"):
        result = await asyncio.to_thread(svc.sidecar_run_start, body)
    else:
        result = await asyncio.to_thread(complete_run, svc,
            None, body)
    return _run_response(result)


async def _recall_snapshot(request: Request) -> JSONResponse:
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    result = await asyncio.to_thread(svc.sidecar_recall_snapshot,
        request.path_params["recall_id"], request.query_params.get("view", "refs"))
    return _generation_response(result)


async def _generation_status(request: Request) -> JSONResponse:
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    return _generation_response(svc.sidecar_generation_status(request.path_params["recall_id"]))


async def _generation_tool_status(request: Request) -> JSONResponse:
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    return _generation_response(svc.sidecar_tool_status(
        request.path_params["recall_id"], request.path_params["tool_call_id"]))


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
        if "recall_id" in body:
            return _generation_response({"status": "failed", "code": "invalid_identity",
                                         "error": "A real user message is required"})
        return JSONResponse({"injected": False, "injected_text": None,
                             "reason": "empty message"})
    if "recall_id" in body and (not isinstance(body["recall_id"], str) or not body["recall_id"].strip()):
        return _generation_response({"status": "failed", "code": "invalid_identity",
                                     "error": "Invalid recall_id"})
    return _generation_response(await asyncio.to_thread(svc.hook_turn_start,
        body.get("session_id"), message, body.get("heart_turn_id"),
        user_message_id=_opt_int(body, "user_message_id"),
        context_start_id=_opt_int(body, "context_start_id"),
        recall_id=body.get("recall_id"), occurred_at=body.get("occurred_at"),
        activity_revision=body.get("activity_revision"), window_revision=body.get("window_revision"),
        context_message_ids=body.get("context_message_ids")))


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
    if "recall_id" in body and (not isinstance(body["recall_id"], str) or not body["recall_id"].strip()):
        return _generation_response({"status": "failed", "code": "invalid_identity",
                                     "error": "Invalid recall_id"})
    if type(body.get("user_is_system", False)) is not bool:
        return _generation_response({"status": "failed", "code": "invalid_arguments",
                                     "error": "user_is_system must be boolean"})
    return _generation_response(await asyncio.to_thread(complete_turn, svc,
        None,
        body.get("session_id"), str(body.get("assistant_reply") or ""),
        turn, assistant_message_id=_opt_int(body, "assistant_message_id"),
        recall_id=body.get("recall_id"), status=body.get("status", "done"),
        user_is_system=body.get("user_is_system", False)))


async def _slice_ingestion_status(request: Request) -> JSONResponse:
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    result = await asyncio.to_thread(IngestionJobs(svc.store.root).status,
                                     request.path_params["job_id"])
    if result is None:
        return JSONResponse({"code": "slice_job_not_found"}, status_code=404)
    return JSONResponse(result)


async def _slice_recall_preview(request: Request) -> JSONResponse:
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict) or not isinstance(body.get("input"), str) or not body["input"].strip():
        return JSONResponse({"code": "invalid_arguments", "error": "input must be nonempty text"}, status_code=400)
    recall = SliceRecall(svc.store.root, svc.source_instance)
    result = await asyncio.to_thread(recall.match, body["input"])
    return JSONResponse(result)


async def _slice_recall_trace(request: Request) -> JSONResponse:
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    result = await asyncio.to_thread(SliceRecall(svc.store.root, svc.source_instance).trace,
        request.path_params["trace_id"], request.query_params.get("view") == "sources")
    return JSONResponse(result if result is not None else {"code": "slice_recall_not_found"},
                        status_code=200 if result is not None else 404)


# ---------- Sleep材料交付与退役入口 ----------

async def _sleep_knowledge(request: Request) -> JSONResponse:
    from ember.sleep_knowledge import SleepKnowledge
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({'status':'failed','code':'invalid_json'},status_code=400)
    operation = request.path_params['operation']
    method = {'start':'start','tool':'call','finish':'finish'}.get(operation)
    if method is None:
        return JSONResponse({'status':'failed','code':'unknown_operation'},status_code=404)
    result = await asyncio.to_thread(getattr(SleepKnowledge(svc),method),body)
    return JSONResponse(result)

async def _retired_sleep_step(request: Request) -> JSONResponse:
    svc = _service_or_404(request)
    if isinstance(svc, JSONResponse):
        return svc
    return JSONResponse({'status':'retired','code':'legacy_sleep_step_retired',
                         'hint':'Use Sleep knowledge materials; judgment and slice/knowledge indexing run independently.'}, status_code=410)


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
    return JSONResponse(await asyncio.to_thread(svc.sidecar_briefing_refresh,body))


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
        Route("/ember/runs/start", _run_lifecycle, methods=["POST"]),
        Route("/ember/runs/finish", _run_lifecycle, methods=["POST"]),
        Route("/ember/runs/{run_id}", _run_lifecycle, methods=["GET"]),
        Route("/ember/recalls/{recall_id}", _recall_snapshot, methods=["GET"]),
        Route("/ember/generations/{recall_id}/tools/{tool_call_id}",
              _generation_tool_status, methods=["GET"]),
        Route("/ember/generations/{recall_id}", _generation_status, methods=["GET"]),
        Route("/ember/hooks/turn_start", _hook_turn_start, methods=["POST"]),
        Route("/ember/hooks/turn_end", _hook_turn_end, methods=["POST"]),
        Route("/ember/slices/jobs/{job_id}", _slice_ingestion_status, methods=["GET"]),
        Route("/ember/recall/slices/preview", _slice_recall_preview, methods=["POST"]),
        Route("/ember/recall/slices/{trace_id}", _slice_recall_trace, methods=["GET"]),
        Route("/ember/sleep/knowledge/{operation}", _sleep_knowledge, methods=["POST"]),
        Route("/ember/sleep/link/batches", _retired_sleep_step,
              methods=["GET"]),
        Route("/ember/sleep/link/submit", _retired_sleep_step,
              methods=["POST"]),
        Route("/ember/sleep/link/progress", _retired_sleep_step,
              methods=["GET"]),
        Route("/ember/sleep/structure/run", _retired_sleep_step,
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
