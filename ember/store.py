# -*- coding: utf-8 -*-
"""Ember v3 MVP — 持久化层与可重建索引。

原则（架构文档 §12）：
- 索引属于工程层，不属于认知数据本体，由 Event / RecallRoute 自动生成。
- 索引可以随时重建。source of truth 是 Event 和 RecallRoute 的存储文件。
- 第一阶段索引只做 exact lexical lookup。
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from typing import Any

from .models import Event, RecallRoute

EVENTS_FILE = "events.jsonl"
ROUTES_FILE = "recall_routes.jsonl"


class EmberStore:
    """文件持久化 + 从存储自动重建的 lexical 索引。"""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self._events: dict[str, Event] = {}
        self._routes: dict[str, RecallRoute] = {}
        self._load()
        # cue(小写) -> [route_id]  ——— 可重建的工程层索引
        self._cue_index: dict[str, set[str]] = defaultdict(set)
        self._rebuild_index()

    # ---------- 持久化 ----------

    def _path(self, name: str) -> str:
        return os.path.join(self.root, name)

    def _load(self) -> None:
        for fname, cls, target in (
            (EVENTS_FILE, Event, self._events),
            (ROUTES_FILE, RecallRoute, self._routes),
        ):
            p = self._path(fname)
            if not os.path.exists(p):
                continue
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    obj = cls.from_dict(json.loads(line))
                    if fname == EVENTS_FILE:
                        target[obj.event_id] = obj
                    else:
                        target[obj.route_id] = obj

    def _append(self, fname: str, obj: Any) -> None:
        with open(self._path(fname), "a", encoding="utf-8") as f:
            f.write(json.dumps(obj.to_dict(), ensure_ascii=False) + "\n")

    # ---------- 索引 ----------

    def _rebuild_index(self) -> None:
        self._cue_index = defaultdict(set)
        for route in self._routes.values():
            if route.status == "active" and route.matcher == "lexical":
                self._cue_index[route.cue.strip().lower()].add(route.route_id)

    def rebuild_index(self) -> None:
        """公开入口：cue 索引重建（revise 改 route 后调用）。"""
        self._rebuild_index()

    def reload(self) -> None:
        """公开入口：从存储文件重载 events/routes（行级 patch 后调用）。"""
        self._load()
        self._rebuild_index()

    def index_snapshot(self) -> dict[str, list[str]]:
        """cue -> route_ids，用于验收/调试查看。"""
        return {k: sorted(v) for k, v in sorted(self._cue_index.items())}

    # ---------- 写入 ----------

    def write_event(self, event: Event) -> Event:
        self._events[event.event_id] = event
        self._append(EVENTS_FILE, event)
        return event

    def write_route(self, route: RecallRoute) -> RecallRoute:
        if route.event_id not in self._events:
            raise KeyError(f"route 指向不存在的 event: {route.event_id}")
        self._routes[route.route_id] = route
        self._append(ROUTES_FILE, route)
        if route.status == "active" and route.matcher == "lexical":
            self._cue_index[route.cue.strip().lower()].add(route.route_id)
        return route

    def retire_route(self, route_id: str) -> None:
        """退休一条 route（第一阶段手动操作，未来归 Sleep 管）。"""
        route = self._routes[route_id]
        route.status = "retired"
        self._append(ROUTES_FILE, route)
        self._rebuild_index()

    # ---------- 读取 ----------

    def get_event(self, event_id: str) -> Event:
        return self._events[event_id]

    def get_route(self, route_id: str) -> RecallRoute:
        return self._routes[route_id]

    def all_events(self) -> list[Event]:
        return list(self._events.values())

    def all_routes(self) -> list[RecallRoute]:
        return list(self._routes.values())

    def routes_for_event(self, event_id: str) -> list[RecallRoute]:
        return [r for r in self._routes.values() if r.event_id == event_id]
