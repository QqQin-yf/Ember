# -*- coding: utf-8 -*-
"""Ember v3 — 梦推送编排（heart 梦通道 v3）。

对齐背景（heart 梦架构 v3，2026-10-10 契约定稿）：
- 时序：必须在入睡确认之后推送（非睡眠档调用 material → 400）。
- 一次性批量：POST /api/dream/material {"events": [...]}，
  收 {"ok": true, "dream_id": "night-xxxx"} 即收工——
  后续组装/散文/判醒/殿堂全在 heart 内部，Ember 不再参与。
- 失败口径（2026-10-10 momo 拍板）：超时重试最多 3 次，仍失败
  上抛报错（统一诊断出口在调用方/前端层）。
- 无梦之夜照推：events=[] → dream_id=null，健康状态，账本照记。

配置（.env）：
- HEART_BASE_URL（如 http://127.0.0.1:8000 或 https://chat.loismclaude.me）
- HEART_AUTH_TOKEN（smoke_config.json 的 auth_token；可空 = 不鉴权）
- HEART_INSTANCE（多实例路由，缺省 default）
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

MAX_RETRIES = 3          # 超时重试上限（momo 2026-10-10 拍板）
TIMEOUT = 30             # 单次请求超时（秒）


class DreamPushError(RuntimeError):
    """推送最终失败（重试耗尽或 heart 拒收）。message 面向诊断出口。"""


class DreamPusher:
    """heart 梦通道的 HTTP 客户端与一夜的推送编排。"""

    def __init__(self, base_url: str | None = None, auth_token: str | None = None,
                 instance: str | None = None):
        self.base_url = (base_url
                         or os.environ.get("HEART_BASE_URL", "")).rstrip("/")
        self.auth_token = auth_token if auth_token is not None \
            else os.environ.get("HEART_AUTH_TOKEN", "")
        self.instance = instance if instance is not None \
            else os.environ.get("HEART_INSTANCE", "default")
        if not self.base_url:
            raise DreamPushError("HEART_BASE_URL 未配置，无法推送梦材料")

    # ---- HTTP ----

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.auth_token:
            h["Authorization"] = f"Bearer {self.auth_token}"
        if self.instance:
            h["X-Heart-Instance"] = self.instance
        return h

    def _post(self, path: str, body: dict | None = None) -> dict[str, Any]:
        """POST 且带重试；4xx（除 429）不重试——heart 拒收重试也无意义。"""
        url = self.base_url + path
        data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        last_err: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                req = urllib.request.Request(
                    url, data=data, headers=self._headers(), method="POST")
                with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as ex:
                detail = ""
                try:
                    fp = getattr(ex, "fp", None)
                    if fp is not None and hasattr(fp, "seek"):
                        try:
                            fp.seek(0)
                        except Exception:
                            pass
                    raw = ex.read()
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    detail = str(raw or "")[:300]
                    # heart 错误响应统一为 {"error": "<原因>"}（回执 r1 §3.2：
                    # 400/401/404 全部单层 error 字段）——提取出原因文本展示
                    try:
                        parsed = json.loads(detail)
                        if isinstance(parsed, dict) and parsed.get("error"):
                            detail = str(parsed["error"])[:300]
                    except (json.JSONDecodeError, ValueError):
                        pass  # 非 JSON body，原文照示
                except Exception:
                    pass
                if ex.code == 429:
                    last_err = DreamPushError(
                        f"heart 限流（HTTP 429）第 {attempt} 次：{detail}")
                    continue
                raise DreamPushError(
                    f"heart 拒收（HTTP {ex.code}）：{detail}") from ex
            except (urllib.error.URLError, TimeoutError, OSError) as ex:
                last_err = DreamPushError(
                    f"heart 不可达（第 {attempt}/{MAX_RETRIES} 次）：{ex}")
                continue
        raise last_err or DreamPushError("heart 推送失败（原因未知）")

    def _get(self, path: str) -> dict[str, Any]:
        url = self.base_url + path
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ---- 一夜的编排 ----

    def push_night(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """推一夜材料（不含选材——选材在 DreamSource）。

        返回 {"dream_id": str|None, "ok": True}。
        dream_id=None = 无梦之夜（健康状态）。
        400（含"heart 未入睡"）不重试直接抛——时序错误重试无意义。
        """
        resp = self._post("/api/dream/material", {"events": events})
        if not resp.get("ok"):
            raise DreamPushError(f"heart 返回 ok=false：{resp}")
        return {"ok": True, "dream_id": resp.get("dream_id")}

    def state(self) -> dict[str, Any]:
        """观测端点：heart 当前状态白名单（确认入睡/清醒档位）。"""
        return self._get("/api/state")

    def dreams(self, captured_only: bool = False) -> dict[str, Any]:
        """观测端点：殿堂（联调确认散文落档用）。"""
        return self._get("/api/dreams/captured" if captured_only
                         else "/api/dreams")
