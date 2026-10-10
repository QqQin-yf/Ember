# -*- coding: utf-8 -*-
"""Ember v3 — 会话原文存档（r4 第 1 项，Claude 交接单 2026-10-10）。

这个文件是干什么的：
  把每轮对话的用户原话与最终回复落盘到 transcripts.jsonl，
  使对话原文不再只活在内存 Session.messages 里——proxy 重启不丢，
  也是重启恢复计数与 read_memory_source 工具的数据来源。

被谁调用：
  - ember/service.py：chat_completion 本轮成功结束后追加两行
    （用户原话 + 最终回复）；chat 失败（messages.pop 分支）不写。
  - ember/service.py：get_or_create_session 新建 Session 时用
    max_turn 恢复 turn 计数（r4 第 2 项）。
  - ember/service.py：read_memory_source 工具按 turn 区间取原文（r4 第 4 项）。

存储形态：append-only jsonl，每行：
  {"session_id", "turn", "role": "user"|"assistant", "content", "time"}
不存 tool_calls / tool 消息（r4 单子约定）。
上下文恢复（messages 历史重建）= momo 2026-10-10 定为独立项，不在此文件。
"""
from __future__ import annotations

import json
import os
from typing import Any

TRANSCRIPTS_FILE = "transcripts.jsonl"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class TranscriptStore:
    """transcripts.jsonl 的追加写与按轮读取。source of truth 是存储文件。"""

    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self) -> str:
        return os.path.join(self.root, TRANSCRIPTS_FILE)

    def append(self, session_id: str, turn: int, role: str, content: str) -> None:
        """追加一行原文。role 限 user/assistant（tool 消息不存）。"""
        if role not in ("user", "assistant"):
            return
        row = {"session_id": session_id, "turn": turn, "role": role,
               "content": content, "time": _now_iso()}
        with open(self._path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def append_turn(self, session_id: str, turn: int,
                    user_text: str, assistant_text: str) -> None:
        """一轮成功对话的两行存档：用户原话（不带召回前缀）+ 最终回复。"""
        self.append(session_id, turn, "user", user_text)
        self.append(session_id, turn, "assistant", assistant_text)

    def get(self, session_id: str, turns: list[int]) -> list[dict[str, Any]]:
        """取指定 session 的若干轮原文，按 turn 升序、同轮内 user 在前。

        无存档/文件不存在 → 空列表（调用方按 not_archived 处理，不报错）。
        """
        want = set(turns)
        if not want:
            return []
        rows: list[dict[str, Any]] = []
        p = self._path()
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("session_id") == session_id and d.get("turn") in want:
                    rows.append(d)
        role_rank = {"user": 0, "assistant": 1}
        rows.sort(key=lambda r: (r.get("turn", 0), role_rank.get(r.get("role"), 9)))
        return rows

    def max_turn(self, session_id: str) -> int:
        """该 session 已存档的最大 turn；无存档返回 0（重启恢复计数用）。"""
        best = 0
        p = self._path()
        if not os.path.exists(p):
            return 0
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("session_id") == session_id:
                    t = d.get("turn")
                    if isinstance(t, int) and t > best:
                        best = t
        return best

    def has_session(self, session_id: str) -> bool:
        return self.max_turn(session_id) > 0
