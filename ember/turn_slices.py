"""Durable per-turn source copies; independent of Events and recall indexing."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone

from . import source_client
from .source_refs import message_seq


class TurnSliceStore:
    def __init__(self, root: str):
        self.path = Path(root) / "turn_slices.sqlite3"

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA secure_delete=ON")
            db.execute("""CREATE TABLE IF NOT EXISTS turn_slices (
                slice_id TEXT PRIMARY KEY,
                instance TEXT NOT NULL,
                session_id TEXT NOT NULL,
                user_message_id TEXT,
                assistant_message_id TEXT NOT NULL,
                user_is_system INTEGER NOT NULL,
                messages_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                saved_at TEXT NOT NULL
            )""")
            with db:
                yield db
        finally:
            db.close()

    def save(self, session_id: str, messages: list[dict], *,
             instance: str = "default", user_is_system: bool = False) -> dict:
        """Store one exact user/reply pair, or an assistant-only turn.

        System-side content is excluded before hashing or persistence. An
        explicit provider marker or caller flag is required for user-role
        system messages; text is never used to guess who sent a message.
        """
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id required")
        if not isinstance(instance, str) or not instance.strip():
            raise ValueError("instance required")
        if type(user_is_system) is not bool:
            raise ValueError("user_is_system must be boolean")
        if not isinstance(messages, list) or len(messages) not in (1, 2):
            raise ValueError("Expected one user/assistant pair or one assistant")
        normalized = []
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("Message must be an object")
            seq = message_seq(message.get("message_id"))
            role, text = message.get("role"), message.get("content")
            if not seq or role not in ("user", "system", "assistant") or not isinstance(text, str):
                raise ValueError("Exact message identity, role and text required")
            normalized.append({"message_id": seq, "role": role, "content": text})
        assistant = normalized[-1]
        if assistant["role"] != "assistant":
            raise ValueError("A completed assistant message is required")
        user = normalized[0] if len(normalized) == 2 else None
        if user and (user["role"] not in ("user", "system") or
                     int(user["message_id"]) >= int(assistant["message_id"])):
            raise ValueError("Expected user/system message followed by its assistant reply")
        omitted = bool(user_is_system or (user and (
            user["role"] == "system" or messages[0].get("is_system") is True)))
        kept = ([user] if user and not omitted else []) + [assistant]
        key = [instance, session_id, user["message_id"] if user else None,
               assistant["message_id"]]
        slice_id = hashlib.sha256(json.dumps(key).encode()).hexdigest()
        body = json.dumps(kept, ensure_ascii=False, sort_keys=True)
        content_hash = hashlib.sha256(body.encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        with self._db() as db:
            db.execute("""INSERT INTO turn_slices VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(slice_id) DO UPDATE SET
                    user_is_system=excluded.user_is_system,
                    messages_json=excluded.messages_json,
                    content_hash=excluded.content_hash,
                    saved_at=excluded.saved_at
                WHERE turn_slices.content_hash != excluded.content_hash
                   OR turn_slices.user_is_system != excluded.user_is_system""",
                (slice_id, instance, session_id, key[2], key[3], int(omitted),
                 body, content_hash, now))
        return self.get(slice_id)

    def get(self, slice_id: str) -> dict | None:
        if not self.path.exists():
            return None
        with self._db() as db:
            row = db.execute("SELECT * FROM turn_slices WHERE slice_id=?", (slice_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["messages"] = json.loads(result.pop("messages_json"))
        result["user_is_system"] = bool(result["user_is_system"])
        return result


def fetch_and_save_turn(store: TurnSliceStore, session_id: str,
                        assistant_message_id: int | str, *,
                        user_message_id: int | str | None = None,
                        instance: str = "default", user_is_system: bool = False) -> dict:
    """Fetch exact original messages before writing; no hook payload fallback."""
    assistant_id = message_seq(assistant_message_id)
    user_id = message_seq(user_message_id) if user_message_id is not None else None
    if not assistant_id or (user_message_id is not None and not user_id):
        raise ValueError("Valid source message IDs required")
    ids = ([user_id] if user_id else []) + [assistant_id]
    if user_id and int(user_id) >= int(assistant_id):
        raise ValueError("User message must precede assistant reply")
    rows = source_client.fetch_source_path(session_id, ids, instance=instance)
    if ([message_seq(row.get("message_id")) for row in rows] != ids or
            rows[-1].get("role") != "assistant" or
            (user_id and rows[0].get("role") != "user")):
        raise source_client.SourceUnavailable("Turn source identities/roles mismatch")
    return store.save(session_id, rows, instance=instance, user_is_system=user_is_system)
