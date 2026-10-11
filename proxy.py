# -*- coding: utf-8 -*-
"""Ember v3 Phase 2 — Ember Proxy（HTTP 层，标准库实现）。

统一入口（momo §2）：
- POST /v1/chat/completions   OpenAI-compatible，附 ember.memory 状态
- GET  /v1/ember/events       已保存 Events 列表
- GET  /v1/ember/events/<id>  完整 Event + Routes（只读）
- POST /v1/ember/recall       手动触发 lexical recall（观察用）
- POST /v1/ember/dream/push   heart 梦通道 v3：选材+批量推送一夜（2026-10-10 契约）
- GET  /v1/ember/dream_preview / dream_ledger   梦选材预览 / 夜账本观察

API Key 只存在于 Proxy（服务端），浏览器永不接触。
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ember.llm import MockLLMClient, OpenAICompatClient
from ember.service import EmberService
from ember.store import EmberStore

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("EMBER_DATA_DIR", os.path.join(HERE, "data"))
STATIC_DIR = os.path.join(HERE, "web")

# ---------- Provider 配置（环境变量；缺省 Mock） ----------
# momo 2026-09-30 裁决：聊天和写记忆共用同一个模型。
# 请求里带 model 参数可按轮切换模型，session 历史不受影响。

PROVIDER = os.environ.get("EMBER_PROVIDER", "mock")          # mock | openai
BASE_URL = os.environ.get("EMBER_CHAT_BASE_URL", "")
API_KEY = os.environ.get("EMBER_CHAT_API_KEY", "")
MODEL = os.environ.get("EMBER_CHAT_MODEL", "glm-5.3-flash")  # 默认单模型
PORT = int(os.environ.get("EMBER_PORT", "8765"))


def build_service() -> EmberService:
    store = EmberStore(DATA_DIR)
    if PROVIDER == "mock":
        print("[ember] provider = MOCK（无真实 LLM，流程演示/测试用）")
        mock = MockLLMClient()
        return EmberService(store, mock, model="mock-chat")
    if not BASE_URL or not API_KEY:
        raise SystemExit("EMBER_PROVIDER=openai 需要设置 EMBER_CHAT_BASE_URL 和 EMBER_CHAT_API_KEY")
    print(f"[ember] provider = openai-compatible  model={MODEL}（chat 与 memory writer 共用）")
    return EmberService(
        store,
        OpenAICompatClient(BASE_URL, API_KEY),
        model=MODEL,
    )


SERVICE = build_service()


class Handler(BaseHTTPRequestHandler):
    server_version = "EmberProxy/0.2"

    def log_message(self, fmt, *args):  # 简洁日志
        print(f"[http] {self.address_string()} {fmt % args}")

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except json.JSONDecodeError:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    # ---------- GET ----------

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            return self._serve_file("index.html", "text/html; charset=utf-8")
        if path == "/app.js":
            return self._serve_file("app.js", "text/javascript; charset=utf-8")
        if path == "/v1/ember/events":
            return self._json(200, {"events": SERVICE.list_events()})
        if path.startswith("/v1/ember/events/"):
            eid = path.rsplit("/", 1)[-1]
            detail = SERVICE.view_event(eid)
            if detail is None:
                return self._json(404, {"error": f"event not found: {eid}"})
            return self._json(200, detail)
        if path == "/v1/ember/turn_ledger":
            return self._json(200, SERVICE.turn_ledger_status())
        if path == "/v1/ember/turn_spans":
            return self._json(200, SERVICE.turn_span_status())
        if path == "/v1/ember/recall_ledger":
            return self._json(200, SERVICE.recall_ledger_status())
        if path == "/v1/ember/dream_ledger":
            return self._json(200, SERVICE.dream_ledger_status())
        if path == "/v1/ember/dream_preview":
            return self._json(200, SERVICE.dream_material_preview())
        return self._json(404, {"error": "not found"})

    def _serve_file(self, name: str, ctype: str) -> None:
        p = os.path.join(STATIC_DIR, name)
        if not os.path.exists(p):
            return self._json(404, {"error": f"missing static file {name}"})
        with open(p, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- POST ----------

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/v1/chat/completions":
            payload = self._read_body()
            try:
                result = SERVICE.chat_completion(payload)
                return self._json(200, result)
            except Exception as ex:  # chat 本身失败 → 502
                return self._json(502, {"error": {"message": str(ex)[:300]}})
        if path == "/v1/ember/recall":
            payload = self._read_body()
            query = payload.get("query") or payload.get("input") or ""
            if not query:
                return self._json(400, {"error": "missing query"})
            return self._json(200, SERVICE.recall(query))
        if path == "/v1/ember/session":
            payload = self._read_body()
            sid = payload.get("session_id") or ""
            msgs = SERVICE.session_messages(sid)
            if msgs is None:
                return self._json(404, {"error": f"session not found: {sid}"})
            return self._json(200, {"session_id": sid, "messages": msgs})
        if path == "/v1/ember/dream/push":
            # heart 梦通道 v3（2026-10-10 契约）：前端点完入睡按钮后调用。
            # Ember 选材 → 一次性批量推送 → 收 dream_id 即收工；
            # 失败（重试耗尽/heart 拒收）返回 502，统一诊断出口在调用方。
            try:
                return self._json(200, SERVICE.push_dream_night())
            except Exception as ex:
                return self._json(502, {"error": str(ex)[:400]})
        return self._json(404, {"error": "not found"})


def main():
    # 绑定地址可配（EMBER_HOST）：默认 127.0.0.1（本机自用）；
    # 同机容器化部署时 app 容器经 docker0 网桥（172.17.0.1）访问，
    # 需设 EMBER_HOST=0.0.0.0（VPS 上配合防火墙/安全组只放行本机访问）
    host = os.environ.get("EMBER_HOST", "127.0.0.1")
    httpd = ThreadingHTTPServer((host, PORT), Handler)
    print(f"[ember] Ember Proxy listening on http://{host}:{PORT}")
    print(f"[ember] UI:     http://{host}:{PORT}/")
    print(f"[ember] API:    http://{host}:{PORT}/v1/chat/completions")
    print(f"[ember] store:  {DATA_DIR}")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
