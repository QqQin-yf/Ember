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
    """(ember_session, ember_turn) <-> heart_turn_id / message_id 映射账本。

    Append-only：每次登记追加一行 jsonl；内存表允许同键覆盖
    （重新生成/编辑重发场景下同一 ember turn 可能先登记 A 又登记 B，
    保留最新映射并原样留痕，语义澄清待 momo 单独裁决）。

    message_id 映射（2026-10-14 去原文化，momo 拍板「写入记来源」）：
    app 在轮前/轮后钩子里带上本轮在后端消息树的行 id（user_message_id /
    assistant_message_id），由本账本登记——Event 本体只持有 ember 坐标
    turn-N（铁律不变），「事件 ↔ 后端原文」的换算经此账本完成：
    - 召回起点过滤：context_start_id（窗口起点 message_id）反查起点
      turn，来源 turn 区间全部 ≥ 起点的候选滤掉（原文就在现场）。
    - 原文拉取：event 的 turn 区间换算成 message_id 区间，向后端
      原文提供接口按需取（transcripts 退役的地基）。
    """
    root: str
    # (session_id, ember_turn) -> heart_turn_id
    mapping: dict[tuple[str, int], str] = field(default_factory=dict)
    # heart_turn_id -> [(ember_session, ember_turn)]（反向索引，可能多值）
    reverse: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    # 已见过的全部 heart turn_id（发号连续性观察用，比如给"重新生成"检测留料）
    seen_heart_ids: set[str] = field(default_factory=set)
    # (session_id, ember_turn) -> user_message_id / assistant_message_id
    user_msg: dict[tuple[str, int], int] = field(default_factory=dict)
    reply_msg: dict[tuple[str, int], int] = field(default_factory=dict)
    # 全部见过的轮（无条件记账的最小行也在内）——turn 计数恢复的依据
    all_turns: set[tuple[str, int]] = field(default_factory=set)

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
                if not sid or not isinstance(et, int):
                    continue
                key = (sid, et)
                self.all_turns.add(key)
                ht = d.get("heart_turn_id") or ""
                if ht:
                    self.mapping[key] = ht
                    self.reverse.setdefault(ht, []).append(key)
                    self.seen_heart_ids.add(ht)
                um = d.get("user_message_id")
                if isinstance(um, int) and um > 0:
                    self.user_msg[key] = max(um, self.user_msg.get(key, 0))
                rm = d.get("assistant_message_id")
                if isinstance(rm, int) and rm > 0:
                    self.reply_msg[key] = max(rm, self.reply_msg.get(key, 0))

    def record(self, ember_session: str, ember_turn: int,
               heart_turn_id: str | None = None,
               user_message_id: int | None = None) -> dict[str, Any] | None:
        """登记一轮映射（轮前钩子/chat 入口调用）。

        2026-10-14 起**无条件记账**：turn 号本身永远落账（最小行）——
        账本接替 transcripts.jsonl 的「重启恢复 turn 计数」职责
        （transcripts 退役，原文只存后端一份）。
        heart_turn_id / user_message_id 有则随行带上。
        """
        ht = str(heart_turn_id).strip() if heart_turn_id else ""
        has_um = isinstance(user_message_id, int) and user_message_id > 0
        entry: dict[str, Any] = {
            "ember_session": ember_session,
            "ember_turn": ember_turn,
            "recorded_at": _now_iso(),
        }
        if ht:
            entry["heart_turn_id"] = ht
        if has_um:
            entry["user_message_id"] = user_message_id
            self.user_msg[(ember_session, ember_turn)] = user_message_id
        with open(self._path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        key = (ember_session, ember_turn)
        self.all_turns.add(key)
        if ht:
            self.mapping[key] = ht
            self.reverse.setdefault(ht, []).append(key)
            self.seen_heart_ids.add(ht)
        return entry

    def max_turn(self, ember_session: str) -> int:
        """该 session 已记账的最大 turn；无记录返回 0（重启恢复计数用）。"""
        best = 0
        for (sid, et) in self.all_turns:
            if sid == ember_session and et > best:
                best = et
        return best

    def record_reply(self, ember_session: str, ember_turn: int,
                     assistant_message_id: int | None) -> dict[str, Any] | None:
        """轮后补记本轮回复的后端行 id（turn_end 钩子调用）。

        与 record 分开两行落账（append-only：先见 user 后见 reply）；
        只要有 message_id 就落账，heart_turn_id 缺失不挡这条。
        """
        if not (isinstance(assistant_message_id, int) and assistant_message_id > 0):
            return None
        entry = {
            "ember_session": ember_session,
            "ember_turn": ember_turn,
            "assistant_message_id": assistant_message_id,
            "recorded_at": _now_iso(),
        }
        with open(self._path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        key = (ember_session, ember_turn)
        self.reply_msg[key] = max(assistant_message_id,
                                  self.reply_msg.get(key, 0))
        return entry

    def turn_of_message(self, ember_session: str, message_id: int) -> int | None:
        """message_id -> 所属 ember turn（精确归属 [user, reply] 区间）。

        拉取的后端原文行标回 ember turn 坐标用（语料同形，定位逻辑
        零改动）；归属不明 → None（该行不进语料）。在飞轮（user 已
        记、reply 未记）只认它自己的 user 行——缺 reply 的轮**不得**
        当作无上界区间（2026-10-17 修，见 turn_at_or_after）。
        """
        for (sid, et), um in self.user_msg.items():
            if sid != ember_session:
                continue
            rm = self.reply_msg.get((sid, et))
            if message_id == um or (rm is not None
                                    and um <= message_id <= rm):
                return et
        return None

    def turn_at_or_after(self, ember_session: str,
                         message_id: int) -> int | None:
        """窗口起点 message_id -> 起点所在的 ember turn。

        判定顺序：
        1. 精确归属：起点落在某轮 [user_msg, reply_msg] 区间内 → 该轮；
           在飞轮（user 已记、reply 未记）只认它自己的 user 行；
        2. 起点比账本覆盖还老（< 最小 user_msg）→ None——无法判定，
           调用方按「无过滤」保守全召回；
        3. 起点落在两轮之间的空隙 → 其后的最早一轮。

        2026-10-17 修：缺 reply 的轮（chat 失败/turn_end 被拒收）
        此前被当作 [user_msg, ∞) 无上界区间——晚期消息 id（截断
        起点）会被吞进最早一个开口轮，keep_from 换算到古老轮，
        生产实测全部记忆被误判现场。开口必须封死：无 reply 的轮
        只认自己的 user 行。
        """
        users = {et: um for (sid, et), um in self.user_msg.items()
                 if sid == ember_session}
        if not users:
            return None
        for et, um in sorted(users.items()):
            rm = self.reply_msg.get((ember_session, et))
            if rm is not None and um <= message_id <= rm:
                return et
            if rm is None and message_id == um:
                return et
        if message_id < min(users.values()):
            return None
        after = [et for et, um in users.items() if um >= message_id]
        return min(after) if after else None

    def message_span(self, ember_session: str,
                     turn_lo: int, turn_hi: int) -> tuple[int, int] | None:
        """ember turn 区间 -> 后端 message_id 区间（原文拉取用）。

        取区间内各轮已知 user/reply 行 id 的极值——放宽无害（多拉的
        原文调用方按需取用）；区间内没有任何映射 → None（不可换算，
        调用方按「宁可少拉不猜」处理）。
        """
        lo: int | None = None
        hi: int | None = None
        for (sid, et), um in self.user_msg.items():
            if sid != ember_session or not (turn_lo <= et <= turn_hi):
                continue
            lo = um if lo is None else min(lo, um)
            hi = um if hi is None else max(hi, um)
        for (sid, et), rm in self.reply_msg.items():
            if sid != ember_session or not (turn_lo <= et <= turn_hi):
                continue
            lo = rm if lo is None else min(lo, rm)
            hi = rm if hi is None else max(hi, rm)
        if lo is None or hi is None:
            return None
        return (lo, hi)

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
