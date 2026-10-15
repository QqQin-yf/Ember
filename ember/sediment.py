# -*- coding: utf-8 -*-
"""Ember v3 — 沉淀证据账本（2026-10-14 设计稿 v2 第 4 步）。

分工（10.15 与 Claude 侧对齐）：沉淀正文与版本号归 app——只有 app
真的存下一版新沉淀时，才把版本号和证据 id 交给 Ember；证据为空
app 根本不调接口。Ember 只做一件事：给某一版沉淀追加证据。

文件：sediment.jsonl（append-only）。每行：
{
  "version": 3,                       # app 传的沉淀版本号（照抄不校验）
  "evidence_event_ids": ["EVT-..."],  # 本版净新增证据（去重后）
  "recorded_at": "ISO 时间"
}

去重纪律（只记改变了沉淀的事件）：
- 本版内去重 + 剔掉其他版本已有的 id（历史并集是"新增"的基准）；
- 同版本重交 → 覆盖为最新一次的列表（"每个版本都写最新的"），
  覆盖后并集现算，旧列表独占的 id 自然退出。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

SEDIMENT_FILE = "sediment.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class SedimentStore:
    """沉淀证据账本：按版本挂证据，append-only + 同版本覆盖。"""

    def __init__(self, root: str):
        self.root = root
        self.versions: dict[int, dict[str, Any]] = {}
        self._load()

    def _path(self) -> str:
        return os.path.join(self.root, SEDIMENT_FILE)

    def _load(self) -> None:
        p = self._path()
        if not os.path.exists(p):
            return
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                v = d.get("version")
                if isinstance(v, int):
                    self.versions[v] = d

    def known_evidence(self, exclude_version: int | None = None) -> set[str]:
        """历史证据并集（exclude_version 用于同版本重交时的基准）。"""
        out: set[str] = set()
        for v, d in self.versions.items():
            if v == exclude_version:
                continue
            out.update(d.get("evidence_event_ids") or [])
        return out

    def append(self, version: int,
               evidence_event_ids: list[str]) -> dict[str, Any]:
        """给某一版沉淀追加证据：去重 → 落行（或返回空集结果不落行）。

        返回 {"kept": 净新增, "duplicates": 剔掉的重复}；kept 为空时
        调用方不应落行（没有净新增就没有可挂的证据）。
        """
        seen: set[str] = set()
        kept: list[str] = []
        dups: list[str] = []
        known = self.known_evidence(exclude_version=version)
        for eid in evidence_event_ids:
            if eid in seen or eid in known:
                dups.append(eid)
                continue
            seen.add(eid)
            kept.append(eid)
        if not kept:
            return {"kept": [], "duplicates": dups}
        entry = {"version": version,
                 "evidence_event_ids": kept,
                 "recorded_at": _now_iso()}
        with open(self._path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self.versions[version] = entry
        return {"kept": kept, "duplicates": dups}

    def latest_version(self) -> int:
        return max(self.versions) if self.versions else 0

    def to_dict(self) -> dict[str, Any]:
        return {"versions": sorted(self.versions),
                "latest_version": self.latest_version(),
                "evidence_total": len(self.known_evidence())}
