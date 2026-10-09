# -*- coding: utf-8 -*-
"""Ember v3 — Turn 映射账本（heart 联动，旁路层）。

对齐背景（momo 2026-10-06）：
- heart 侧每轮请求在 payload 附 heart_meta.turn_id（"t-000001" 式，
  prepare 时全局发号），开关 forward_turn_meta 默认关，Ember 侧就绪后打开。
- Ember 的 turn-N 是会话内自增序数；两套计数器数同一串请求，
  由本层记账对齐——纯管道层映射，零 LLM、零猜测、可重建。

铁律：
- 账本是旁路文件（turn_ledger.jsonl），绝不写进 Event 本体
  （Event 只持有 turn_refs 里的 ember 侧坐标 turn-N）。
- heart 的感受数据（arousal/tension/pa/na）不进本系统——那是 heart
  的 echo_digest 的职责；Ember 只记"哪一轮对应哪一轮"。
- 映射缺失不阻断主流程：heart_meta 没带 turn_id 时照常运行。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

LEDGER_FILE = "turn_ledger.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class TurnLedger:
    """(ember_session, ember_turn) <-> heart_turn_id 映射账本。

    Append-only：每次登记追加一行 jsonl；内存表允许同键覆盖
    （重新生成/编辑重发场景下同一 ember turn 可能先登记 A 又登记 B，
    保留最新映射并原样留痕，语义澄清待 momo 单独裁决）。
    """
    root: str
    # (session_id, ember_turn) -> heart_turn_id
    mapping: dict[tuple[str, int], str] = field(default_factory=dict)
    # heart_turn_id -> [(ember_session, ember_turn)]（反向索引，可能多值）
    reverse: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    # 已见过的全部 heart turn_id（发号连续性观察用，比如给"重新生成"检测留料）
    seen_heart_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self._load()

    def _path(self) -> str:
        return os.path.join(self.root, LEDGER_FILE)

    def _load(self) -> None:
        p = self._path()
        if not os.path.exists(p):
            return
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                sid = d.get("ember_session") or ""
                et = d.get("ember_turn")
                ht = d.get("heart_turn_id") or ""
                if not sid or not isinstance(et, int) or not ht:
                    continue
                key = (sid, et)
                self.mapping[key] = ht
                self.reverse.setdefault(ht, []).append(key)
                self.seen_heart_ids.add(ht)

    def record(self, ember_session: str, ember_turn: int,
               heart_turn_id: str | None) -> dict[str, Any] | None:
        """登记一轮映射；heart 未带 turn_id 时返回 None（不写账）。"""
        if not heart_turn_id:
            return None
        ht = str(heart_turn_id).strip()
        if not ht:
            return None
        entry = {
            "ember_session": ember_session,
            "ember_turn": ember_turn,
            "heart_turn_id": ht,
            "recorded_at": _now_iso(),
        }
        with open(self._path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        key = (ember_session, ember_turn)
        self.mapping[key] = ht
        self.reverse.setdefault(ht, []).append(key)
        self.seen_heart_ids.add(ht)
        return entry

    def heart_id_of(self, ember_session: str, ember_turn: int) -> str | None:
        """ember 坐标 -> heart turn_id；未登记返回 None。"""
        return self.mapping.get((ember_session, ember_turn))

    def ember_turns_of(self, heart_turn_id: str) -> list[tuple[str, int]]:
        """heart turn_id -> ember 坐标（可能多值：重发场景）。"""
        return list(self.reverse.get(str(heart_turn_id), []))

    def gaps(self) -> list[str]:
        """发号连续性观察：已见 heart id 里序数缺失的段（重开/丢弃轮的痕迹）。

        只做观察上报，不做任何修补——脏数据如何处理等 momo 裁决。
        """
        nums = []
        for ht in self.seen_heart_ids:
            try:
                nums.append(int(str(ht).lstrip("t-")))
            except ValueError:
                continue
        if not nums:
            return []
        nums.sort()
        return [f"t-{n:06d}" for n in range(nums[0], nums[-1] + 1)
                if f"t-{n:06d}" not in self.seen_heart_ids]

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": len(self.mapping),
            "heart_ids_seen": len(self.seen_heart_ids),
            "gaps": self.gaps(),
        }
