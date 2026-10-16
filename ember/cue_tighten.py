# -*- coding: utf-8 -*-
"""Ember v3 — 旧 Cue 收紧（Sleep 调用②第三步，纯代码无 LLM）。

对齐（架构文档 §4.2 第三步，2026-10-09 定稿）：

| 项         | 定稿                                                   |
|------------|--------------------------------------------------------|
| 时间条件   | 事件静置满一周                                         |
| 活跃豁免   | Entity open 期间，其下任何 Event 的 Cue 都不收         |
| 起算点     | 有 Entity 的事件：Entity 被 close 的时刻（closed_at）；|
|            | 散事件（无 entity_refs）：事件写入时刻（ingest_time）  |
| 判据（唯一）| Cue 被用到的次数——次数多就留，没用上过就 retire      |
| 整组保护   | 事件名下 Cue 全部零次使用 → 整组不动（事件至少保留     |
|            | 一个入口，不因全零而失去所有记忆触点）                 |
| 依据数据   | 召回记账（recall_ledger，与冷却机制共用）；计数口径 = event_ids 含本事件且 cue 文本匹配的行（精确到事件，不按文本全局聚合） |

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
    """旧 Cue 收紧：读 Event 归属/生命周期 + 召回记账，按事件分组判定。

    Event↔Entity 归属读 events.jsonl 侧表字段 entity_refs（与
    recall.register_event_entity_refs 同源，独立读避免依赖其全局态）。
    判定粒度 = 事件：每个事件独立确定起算点与整组保护。
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

    def _load_events_info(self) -> dict[str, dict]:
        """event_id -> {refs: [entity_id], ingest_time: str}（events.jsonl）。"""
        out: dict[str, dict] = {}
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
                out[d["event_id"]] = {
                    "refs": list(d.get("entity_refs") or []),
                    "ingest_time": d.get("ingest_time") or "",
                }
        return out

    # ---------- 主流程 ----------

    def tighten(self, now: datetime | None = None) -> dict[str, Any]:
        """跑一遍收紧，返回报告（retire 动作由 store 落盘）。

        报告结构（全部按事件粒度）：
          checked_events        检查的事件数
          skipped               [{event_id, reason}]——open 豁免 / 静置不足 /
                                ingest_time 或 closed_at 不可解析
          retired               [{route_id, cue, event_id, uses}]
          kept                  [{route_id, cue, uses}]
          kept_zero_use_event   [{event_id, cue_count}]——整组零次使用，
                                本轮不动（保底：事件至少留一个入口）
        """
        now = now or _now()
        events_info = self._load_events_info()
        report: dict[str, Any] = {"checked_events": 0, "skipped": [],
                                  "retired": [], "kept": [],
                                  "kept_zero_use_event": []}

        for eid, info in events_info.items():
            report["checked_events"] += 1
            refs = info["refs"]
            routes = [r for r in self.store.routes_for_event(eid)
                      if r.status == "active"]
            if not routes:
                continue   # 无 active cue，无事可做

            # ---- 起算点：两套 ----
            if refs:
                # 有 Entity 归属：open 豁免；closed 以 closed_at 起算
                try:
                    ent = self.entity_store.get(refs[0])
                except KeyError:
                    report["skipped"].append(
                        {"event_id": eid,
                         "reason": f"归属的 Entity 不存在: {refs[0]}"})
                    continue
                if ent.status == "open":
                    report["skipped"].append(
                        {"event_id": eid, "reason": "open（Entity 活跃豁免）"})
                    continue
                anchor = _parse_iso(ent.closed_at)
                if anchor is None:
                    report["skipped"].append(
                        {"event_id": eid, "reason": "closed_at 缺失或不可解析"})
                    continue
            else:
                # 散事件：以写入时刻起算
                anchor = _parse_iso(info["ingest_time"])
                if anchor is None:
                    report["skipped"].append(
                        {"event_id": eid, "reason": "ingest_time 缺失或不可解析"})
                    continue

            # ---- 起算点融合最后提取（2026-10-15：提取 = 召回 ∪ 检索，
            # 刷新一切计时器）——idle 从「原起算点」与「最后提取时间」
            # 中较晚者起算，在用的永不收紧 ----
            last_at = (self.ledger.last_extracted.get(eid) or "")
            last_dt = _parse_iso(last_at) if last_at else None
            if last_dt is not None and last_dt > anchor:
                anchor = last_dt

            idle = (now - anchor).total_seconds() / 86400.0
            if idle < self.idle_days:
                report["skipped"].append(
                    {"event_id": eid,
                     "reason": f"静置 {idle:.1f} 天 < {self.idle_days}"})
                continue

            # ---- 判据：按事件分组 ----
            # 使用次数按精确口径：只数 event_ids 里确实含本事件的 cue 行
            # （同文本 cue 挂多个事件时，不借别人的使用记录保自己）
            uses = [(r, self.ledger.cue_use_count_for_event(eid, r.cue))
                    for r in routes]
            if all(u <= self.min_uses for _, u in uses):
                # 整组零次使用 → 整组不动（事件至少保留一个入口）
                report["kept_zero_use_event"].append(
                    {"event_id": eid, "cue_count": len(routes)})
                continue
            for r, u in uses:
                if u <= self.min_uses:
                    self.store.retire_route(r.route_id)
                    report["retired"].append(
                        {"route_id": r.route_id, "cue": r.cue,
                         "event_id": eid, "uses": u})
                else:
                    report["kept"].append(
                        {"route_id": r.route_id, "cue": r.cue, "uses": u})

        return report
