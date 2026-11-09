"""Instance-local generation identity, tool receipts and recall snapshots.

Recall responses expire after 24h; identity and reference metadata remain.
JSONL memory-tool writes are not transactional with SQLite: an accepted call without a committed receipt is unknown, never
blindly replayed. One service process owns each instance directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Any

GENERATIONS_FILE = "generations.sqlite3"
SNAPSHOT_TTL_SECONDS = 24 * 60 * 60


def fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def identity(value: Any) -> str | None:
    if isinstance(value, str) and value.strip() and len(value) <= 512:
        return value.strip()
    return None


class GenerationStore:
    def __init__(self, root: str, recover: bool = True):
        self.path = os.path.join(root, GENERATIONS_FILE)
        if recover and os.path.exists(self.path):
            # Process-local execution contexts cannot survive a restart. Keep
            # historical identities/receipts, but do not reopen live writers.
            with self._db() as db:
                db.execute("UPDATE generations SET state='interrupted' "
                           "WHERE state IN ('starting','running')")
                db.execute("UPDATE calls SET state='unknown' WHERE state='in_progress'")
                db.execute("UPDATE recall_snapshots SET state='unknown' WHERE state='in_progress'")
            self.expire_snapshots()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA secure_delete=ON")
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS generations (
                    recall_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    turn INTEGER,
                    state TEXT NOT NULL,
                    superseded_by TEXT,
                    end_hash TEXT,
                    end_result TEXT,
                    assistant_message_id TEXT,
                    domain TEXT NOT NULL DEFAULT 'app'
                );
                CREATE TABLE IF NOT EXISTS recall_snapshots (
                    recall_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    completed_at REAL,
                    expires_at REAL,
                    metadata_json TEXT,
                    response_json TEXT
                );
                CREATE INDEX IF NOT EXISTS snapshot_expiry ON recall_snapshots(expires_at);
                CREATE TABLE IF NOT EXISTS source_completions (
                    source_hash TEXT NOT NULL,
                    turn INTEGER NOT NULL,
                    binding_json TEXT NOT NULL,
                    PRIMARY KEY(source_hash, turn)
                );
                CREATE TABLE IF NOT EXISTS recall_sources (
                    recall_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    candidates_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recall_effects (
                    operation_id TEXT PRIMARY KEY,
                    recall_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    entry_json TEXT,
                    applied INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS calls (
                    tool_call_id TEXT NOT NULL,
                    recall_id TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_json TEXT,
                    volatile INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (recall_id, tool_call_id)
                );
            """)
            with db:
                columns = {row["name"] for row in db.execute("PRAGMA table_info(generations)")}
                if "domain" not in columns:
                    db.execute("BEGIN IMMEDIATE")
                    columns = {row["name"] for row in db.execute("PRAGMA table_info(generations)")}
                    if "domain" not in columns:
                        db.execute("ALTER TABLE generations ADD COLUMN domain TEXT NOT NULL DEFAULT 'app'")
                yield db
        finally:
            db.close()

    def generation(self, recall_id: str) -> dict | None:
        if not os.path.exists(self.path):
            return None
        with self._db() as db:
            row = db.execute("SELECT * FROM generations WHERE recall_id=?", (recall_id,)).fetchone()
            return dict(row) if row else None

    def reserve(self, recall_id: str, session_id: str, request_hash: str,
                turn: int | None = None, snapshot_metadata: dict | None = None, *,
                domain: str = "app", historical: bool = False) -> None:
        with self._db() as db:
            if not historical:
                db.execute("UPDATE generations SET state='superseded', superseded_by=? "
                           "WHERE domain=? AND state IN ('starting','running')", (recall_id, domain))
            state = "running" if domain == "cc" and not historical else "starting"
            db.execute("INSERT INTO generations(recall_id, session_id, request_hash, turn, state, domain) "
                       "VALUES (?,?,?,?,?,?)", (recall_id, session_id, request_hash, turn, state, domain))
            if snapshot_metadata is not None:
                db.execute("INSERT INTO recall_snapshots(recall_id,state,created_at,metadata_json) "
                           "VALUES (?,'in_progress',?,?)", (recall_id, time.time(),
                               json.dumps(snapshot_metadata, ensure_ascii=False)))

    def supersede(self, domain: str = "app") -> None:
        if os.path.exists(self.path):
            with self._db() as db:
                db.execute("UPDATE generations SET state='superseded' "
                           "WHERE domain=? AND state IN ('starting','running')", (domain,))

    def interrupt(self, recall_id: str) -> None:
        with self._db() as db:
            db.execute("UPDATE generations SET state='interrupted' WHERE recall_id=?", (recall_id,))
            db.execute("UPDATE recall_snapshots SET state='unknown' "
                       "WHERE recall_id=? AND state='in_progress'", (recall_id,))

    def finish(self, recall_id: str, status: str, end_hash: str,
               result: dict, assistant_message_id: str | None) -> None:
        with self._db() as db:
            db.execute("UPDATE generations SET state=?, end_hash=?, end_result=?, "
                       "assistant_message_id=? WHERE recall_id=?",
                       (status, end_hash, json.dumps(result, ensure_ascii=False),
                        assistant_message_id, recall_id))

    def call(self, recall_id: str, tool_call_id: str) -> dict | None:
        if not os.path.exists(self.path):
            return None
        with self._db() as db:
            row = db.execute("SELECT * FROM calls WHERE recall_id=? AND tool_call_id=?", (recall_id, tool_call_id)).fetchone()
            return dict(row) if row else None

    def begin_call(self, recall_id: str, tool_call_id: str, request_hash: str,
                   tool_name: str) -> None:
        with self._db() as db:
            db.execute("INSERT INTO calls(tool_call_id, recall_id, request_hash, tool_name, state) "
                       "VALUES (?,?,?,?,'in_progress')", (tool_call_id, recall_id, request_hash, tool_name))

    def complete_call(self, recall_id: str, tool_call_id: str, result: dict, volatile: bool = False) -> None:
        with self._db() as db:
            db.execute("UPDATE calls SET state='completed', result_json=?, volatile=? "
                       "WHERE recall_id=? AND tool_call_id=?", (None if volatile else json.dumps(result, ensure_ascii=False),
                                               int(volatile), recall_id, tool_call_id))

    def unknown_call(self, recall_id: str, tool_call_id: str) -> None:
        with self._db() as db:
            db.execute("UPDATE calls SET state='unknown' WHERE recall_id=? AND tool_call_id=?", (recall_id, tool_call_id))

    def calls_for(self, recall_id: str) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with self._db() as db:
            return [dict(r) for r in db.execute(
                "SELECT tool_call_id, tool_name, state FROM calls WHERE recall_id=? ORDER BY rowid",
                (recall_id,))]

    def max_turn(self, session_id: str) -> int:
        if not os.path.exists(self.path):
            return 0
        with self._db() as db:
            row = db.execute("SELECT MAX(turn) FROM generations WHERE session_id=?", (session_id,)).fetchone()
            return row[0] or 0

    def commit_snapshot(self, recall_id: str, turn: int, response: dict,
                        metadata: dict, effects: list[tuple[str, dict]], *, historical: bool = False) -> None:
        now = time.time()
        with self._db() as db:
            changed = db.execute("UPDATE recall_snapshots SET state='ready', completed_at=?, "
                "expires_at=?, metadata_json=?, response_json=? WHERE recall_id=? AND state='in_progress'",
                (now, now + SNAPSHOT_TTL_SECONDS, json.dumps(metadata, ensure_ascii=False),
                 json.dumps(response, ensure_ascii=False), recall_id)).rowcount
            if changed != 1:
                raise ValueError("Recall snapshot is not awaiting a first commit")
            db.execute("UPDATE generations SET turn=?, state=? WHERE recall_id=?",
                       (turn, "registered" if historical else "running", recall_id))
            for index, (kind, entry) in enumerate(effects):
                operation_id = fingerprint({"recall_id": recall_id, "effect": index})
                db.execute("INSERT INTO recall_effects(operation_id,recall_id,kind,entry_json) "
                           "VALUES (?,?,?,?)", (operation_id, recall_id, kind,
                                                json.dumps(entry, ensure_ascii=False)))

    def snapshot(self, recall_id: str) -> dict | None:
        if not os.path.exists(self.path):
            return None
        self.expire_snapshots()
        with self._db() as db:
            row = db.execute("SELECT * FROM recall_snapshots WHERE recall_id=?", (recall_id,)).fetchone()
            return dict(row) if row else None

    def pending_effects(self, recall_id: str | None = None) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with self._db() as db:
            sql = "SELECT * FROM recall_effects WHERE applied=0"
            params = ()
            if recall_id is not None:
                sql += " AND recall_id=?"
                params = (recall_id,)
            return [dict(row) for row in db.execute(sql + " ORDER BY rowid", params)]

    def acknowledge_effect(self, operation_id: str) -> None:
        with self._db() as db:
            db.execute("UPDATE recall_effects SET applied=1,entry_json=NULL WHERE operation_id=?", (operation_id,))

    def expire_snapshots(self) -> int:
        if not os.path.exists(self.path):
            return 0
        with self._db() as db:
            return db.execute("UPDATE recall_snapshots SET response_json=NULL "
                "WHERE response_json IS NOT NULL AND expires_at<=?", (time.time(),)).rowcount
