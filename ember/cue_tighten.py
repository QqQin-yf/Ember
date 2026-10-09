# -*- coding: utf-8 -*-
"""Ember v3 — 旧 Cue 收紧（Sleep 调用②第三步，纯代码无 LLM）。

对齐（架构文档 §4.2 第三步，2026-10-08 定稿）：

| 项         | 定稿                                              |
|------------|---------------------------------------------------|
| 时间条件   | 事件静置最多一周                                  |
| 活跃豁免   | Entity open 期间，其下任何 Event 的 Cue 都不收    |
| 起算点     | Entity 被 close 的时刻（closed_at）               |
| 判据（唯一）| Cue 被用到的次数——次数多就留，没用上过就 retire |
| 依据数据   | 召回记账（recall_ledger，与冷却机制共用）         |

收紧不是机械 decay，而是认知意义变了：项目完成后，当初一大把宽泛的
Cue 收敛到少数几个可靠入口。动作只有 retire（status=retired），
没有删除——retired 的 Cue 仍在 routes.jsonl 里留痕。

铁律：
- 本模块所有默认值（静置 7 天、retire 阈值=0 次使用）都是 baseline
  假设，不是已证明设计。
- 收紧是消费端：只读记账与 Entity 状态，不写记忆、不判关系。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

IDLE_DAYS_DEFAULT = 7.0   # 静置一周（baseline）
MIN_USES_DEFAULT = 0      # 使用次数低于该值即 retire（0 = 没用上过就收，baseline）


def _parse_iso(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


class CueTightener:
    """旧 Cue 收紧：读 Entity 生命周期 + 召回记账，retire 不再需要的 Cue。

    Event↔Entity 归属读 events.jsonl 侧表字段 entity_refs（与
    recall.register_event_entity_refs 同源，独立读避免依赖其全局态）。
    """

    def __init__(self, store, ledger, events_path: str,
                 entity_store=None,
                 idle_days: float = IDLE_DAYS_DEFAULT,
                 min_uses: int = MIN_USES_DEFAULT):
        self.store = store
        self.ledger = ledger
        self.events_path = events_path
        # Entity 生命周期来自 EntityStore（与 route store 分离）
        self.entity_store = entity_store or getattr(store, "entity_store", None)
        if self.entity_store is None:
            raise ValueError("需要 EntityStore（entity_store 参数或 store.entity_store）")
        self.idle_days = idle_days
        self.min_uses = min_uses

    # ---------- 数据装载 ----------

    def _event_entity_map(self) -> dict[str, list[str]]:
        """event_id -> [entity_id, ...]（events.jsonl 侧表）。"""
        out: dict[str, list[str]] = {}
        if not os.path.exists(self.events_path):
            return out
        with open(self.events_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                refs = d.get("entity_refs")
                if refs:
                    out[d["event_id"]] = list(refs)
        return out

    # ---------- 主流程 ----------

    def tighten(self, now: datetime | None = None) -> dict[str, Any]:
        """跑一遍收紧，返回报告（不落盘报告本身，retire 动作由 store 落盘）。"""
        now = now or _now()
        ent_map = self._event_entity_map()
        report: dict[str, Any] = {"checked_entities": 0, "skipped": [],
                                  "tightened": [], "retired": [],
                                  "kept": []}

        for entity in self.entity_store.all():
            report["checked_entities"] += 1

            # 活跃豁免：Entity open 期间其下任何 Cue 都不收
            if entity.status == "open":
                report["skipped"].append(
                    {"entity_id": entity.entity_id, "reason": "open"})
                continue

            # 时间条件：起算点 = closed_at，静置不足一周不收
            closed_at = _parse_iso(entity.closed_at)
            if closed_at is None:
                report["skipped"].append(
                    {"entity_id": entity.entity_id,
                     "reason": "closed_at 缺失或不可解析"})
                continue
            idle = (now - closed_at).total_seconds() / 86400.0
            if idle < self.idle_days:
                report["skipped"].append(
                    {"entity_id": entity.entity_id,
                     "reason": f"静置 {idle:.1f} 天 < {self.idle_days}"})
                continue

            # 该 Entity 名下全部 active route
            entity_routes = []
            for ev_id, refs in ent_map.items():
                if entity.entity_id not in refs:
                    continue
                entity_routes.extend(self.store.routes_for_event(ev_id))

            retired_here: list[dict[str, str]] = []
            for route in entity_routes:
                if route.status != "active":
                    continue
                # 判据（唯一）：Cue 被用到的次数（读召回记账）
                uses = self.ledger.cue_use_count.get(route.cue, 0)
                if uses <= self.min_uses:
                    self.store.retire_route(route.route_id)
                    retired_here.append(
                        {"route_id": route.route_id, "cue": route.cue,
                         "event_id": route.event_id, "uses": uses})
                    report["retired"].append(retired_here[-1])
                else:
                    report["kept"].append(
                        {"route_id": route.route_id, "cue": route.cue,
                         "uses": uses})

            if retired_here:
                report["tightened"].append(
                    {"entity_id": entity.entity_id,
                     "closed_at": entity.closed_at,
                     "idle_days": round(idle, 1),
                     "retired_count": len(retired_here)})

        return report
