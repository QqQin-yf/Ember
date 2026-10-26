"""Independent wake execution ledger. No ordinary turns, recall, echo or Event writes."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime

from .generations import fingerprint, identity
from .source_refs import message_seq as _message_seq

RUNS_FILE = "runs.sqlite3"
TERMINAL = {"done", "cancelled", "error"}
READ_TOOLS = {"read_memory_source"}
MAX_READ_RESULTS = 256


def message_seq(value):
    try:
        return _message_seq(value)
    except (ValueError, OverflowError):
        return None


def failure(code, error, field=None):
    result = {"status": "failed", "code": code, "error": error}
    if field is not None:
        result["field"] = field
    return result


def _date(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo is not None else None
    except ValueError:
        return None


def validate(body, phase):
    if not isinstance(body, dict):
        return failure("invalid_arguments", "Expected an object", "$")
    common = {"event_id", "kind", "run_id", "wake_id", "session_id"}
    extra = ({"activity_revision", "window_revision", "leaf_id", "context_start_id", "messages", "occurred_at"}
             if phase == "start" else {"status", "spoke", "assistant_message_id", "assistant_reply", "tools", "finished_at"})
    for key in sorted(body.keys() - common - extra):
        return failure("invalid_arguments", "Undeclared field", key)
    for key in sorted(common | extra):
        if key not in body:
            return failure("invalid_arguments", "Required field missing", key)
    for key in ("event_id", "run_id", "wake_id", "session_id"):
        if identity(body[key]) != body[key] or not isinstance(body[key], str):
            return failure("invalid_arguments", "Expected a nonempty stable identity", key)
    if body["kind"] != "wake":
        return failure("invalid_arguments", "Expected kind=wake", "kind")
    if phase == "start":
        for key in ("activity_revision", "window_revision"):
            value = body[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**53 - 1:
                return failure("invalid_arguments", "Expected a nonnegative safe integer", key)
        for key in ("leaf_id", "context_start_id"):
            if body[key] is not None and (not isinstance(body[key], str) or identity(body[key]) != body[key]):
                return failure("invalid_arguments", "Expected opaque message UUID or null; not a seq", key)
        messages = body["messages"]
        if not isinstance(messages, list) or len(messages) > 10000:
            return failure("invalid_arguments", "Expected at most 10000 real message identities", "messages")
        seen = set()
        for i, row in enumerate(messages):
            path = f"messages[{i}]"
            if not isinstance(row, dict) or set(row) != {"message_id", "role"}:
                return failure("invalid_arguments", "Only message_id and role are accepted; no text/reminder", path)
            seq = message_seq(row["message_id"])
            if seq is None or seq in seen:
                return failure("invalid_arguments", "Invalid or repeated message seq", path + ".message_id")
            seen.add(seq)
            if row["role"] not in ("user", "assistant"):
                return failure("invalid_arguments", "Only real user/assistant roles", path + ".role")
        time_field = "occurred_at"
    else:
        if not isinstance(body["status"], str) or body["status"] not in TERMINAL:
            return failure("invalid_arguments", "Expected done/cancelled/error", "status")
        if not isinstance(body["spoke"], bool):
            return failure("invalid_arguments", "Expected boolean", "spoke")
        if not isinstance(body["assistant_reply"], str):
            return failure("invalid_arguments", "Expected real reply text or empty string", "assistant_reply")
        if body["spoke"]:
            if body["status"] != "done" or message_seq(body["assistant_message_id"]) is None or not body["assistant_reply"].strip():
                return failure("invalid_arguments", "Visible completion needs done, a committed assistant seq and real text", "assistant_message_id")
        elif body["assistant_message_id"] is not None or body["assistant_reply"] != "":
            return failure("invalid_arguments", "Quiet/cancelled/error needs null seq and empty reply", "assistant_message_id")
        tools = body["tools"]
        if not isinstance(tools, list) or len(tools) > 1000:
            return failure("invalid_arguments", "Expected at most 1000 tool metadata records", "tools")
        seen = set()
        for i, row in enumerate(tools):
            path = f"tools[{i}]"
            if not isinstance(row, dict) or set(row) != {"tool_call_id", "name", "status", "result_ref"}:
                return failure("invalid_arguments", "Expected tool_call_id/name/status/result_ref only", path)
            for key in ("tool_call_id", "name", "result_ref"):
                if not isinstance(row[key], str) or identity(row[key]) != row[key]:
                    return failure("invalid_arguments", "Expected nonempty metadata identity", path + "." + key)
            if row["result_ref"] != f"wake-tool:{body['run_id']}:{row['tool_call_id']}":
                return failure("invalid_arguments", "Expected App-local wake-tool:<run_id>:<tool_call_id> reference", path + ".result_ref")
            if row["tool_call_id"] in seen or row["status"] not in ("completed", "failed", "unknown"):
                return failure("invalid_arguments", "Repeated tool identity or invalid status", path)
            seen.add(row["tool_call_id"])
        time_field = "finished_at"
    if _date(body[time_field]) is None:
        return failure("invalid_arguments", "Expected ISO timestamp with timezone", time_field)
    try:
        fingerprint(body)
    except (TypeError, ValueError):
        return failure("invalid_arguments", "Expected finite JSON")
    return None


class RunStore:
    def __init__(self, root):
        self.path = os.path.join(root, RUNS_FILE)
        self.lock = threading.RLock()
        self.live = set()
        self.results = OrderedDict()
        # No recovery mutations: running/finishing identities survive a restart.

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE IF NOT EXISTS runs(
                run_id TEXT PRIMARY KEY, wake_id TEXT UNIQUE NOT NULL,
                session_id TEXT NOT NULL, state TEXT NOT NULL,
                start_json TEXT NOT NULL, finish_json TEXT);
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs((1))
                WHERE state IN ('running','finishing');
            CREATE TABLE IF NOT EXISTS run_operations(
                event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                phase TEXT NOT NULL, request_hash TEXT NOT NULL, response_json TEXT);
            CREATE TABLE IF NOT EXISTS run_calls(
                run_id TEXT NOT NULL, tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL, request_hash TEXT NOT NULL,
                state TEXT NOT NULL, outcome_status TEXT, outcome_code TEXT,
                PRIMARY KEY(run_id,tool_call_id));
        """)
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def response(row, phase):
        start = json.loads(row["start_json"])
        result = {"run_id": row["run_id"], "wake_id": row["wake_id"], "session_id": row["session_id"],
                  "kind": "wake", "origin": "assistant_initiated",
                  "status": "running" if phase == "start" else row["state"],
                  "allowed_tools": sorted(READ_TOOLS), "memory_writes": False,
                  "protocol_version": "ember-runs/1", "window_evidence": "app_attested",
                  **{key: start[key] for key in ("activity_revision", "window_revision", "leaf_id", "context_start_id", "occurred_at")}}
        if phase == "start":
            result["event_id"] = start["event_id"]
        if phase != "start" and row["finish_json"]:
            finish = json.loads(row["finish_json"])
            result.update(event_id=finish["event_id"], spoke=finish["spoke"], assistant_message_id=finish["assistant_message_id"],
                          tools=finish["tools"], finished_at=finish["finished_at"], tool_evidence="app_metadata_only")
        return result

    @staticmethod
    def operation(db, body, phase):
        old = db.execute("SELECT * FROM run_operations WHERE event_id=?", (body["event_id"],)).fetchone()
        if old is None:
            return None
        if old["phase"] != phase or old["run_id"] != body["run_id"] or old["request_hash"] != fingerprint(body):
            return failure("id_conflict", "Operation identity already names different content")
        return json.loads(old["response_json"]) if old["response_json"] is not None else None

    def start(self, body):
        error = validate(body, "start")
        if error:
            return error
        with self.lock:
            with self.db() as db:
                replay = self.operation(db, body, "start")
                if replay is not None:
                    return replay
                if db.execute("SELECT 1 FROM runs WHERE run_id=? OR wake_id=?", (body["run_id"], body["wake_id"])).fetchone():
                    return failure("id_conflict", "Run/wake already registered under another start operation")
                if db.execute("SELECT 1 FROM runs WHERE state IN ('running','finishing')").fetchone():
                    return failure("run_busy", "An active or unresolved run owns this instance slot")
                db.execute("INSERT INTO runs VALUES (?,?,?,'running',?,NULL)",
                           (body["run_id"], body["wake_id"], body["session_id"], json.dumps(body)))
                row = db.execute("SELECT * FROM runs WHERE run_id=?", (body["run_id"],)).fetchone()
                response = self.response(row, "start")
                db.execute("INSERT INTO run_operations VALUES (?,?,?, ?,?)", (body["event_id"],body["run_id"],"start",fingerprint(body),json.dumps(response)))
            self.live.add(body["run_id"])
            return response

    def finish(self, body, verify_reply):
        error = validate(body, "finish")
        if error:
            return error
        if not os.path.exists(self.path):
            return failure("run_not_found", "No such run in this instance")
        with self.lock:
            with self.db() as db:
                replay = self.operation(db, body, "finish")
                if replay is not None:
                    return replay
                row = db.execute("SELECT * FROM runs WHERE run_id=?", (body["run_id"],)).fetchone()
                if row is None:
                    return failure("run_not_found", "No such run in this instance")
                if row["session_id"] != body["session_id"] or row["wake_id"] != body["wake_id"]:
                    return failure("context_mismatch", "Run/session/wake mismatch")
                if row["state"] in TERMINAL:
                    return failure("run_finished", "First terminal result is immutable")
                start = json.loads(row["start_json"])
                if _date(body["finished_at"]) < _date(start["occurred_at"]):
                    return failure("invalid_arguments", "finished_at precedes occurred_at", "finished_at")
                intent = db.execute("SELECT * FROM run_operations WHERE run_id=? AND phase='finish'", (body["run_id"],)).fetchone()
                if intent is not None and intent["event_id"] != body["event_id"]:
                    return failure("id_conflict", "An unresolved finish intent already exists")
                if intent is None:
                    db.execute("INSERT INTO run_operations VALUES (?,?,?, ?,NULL)", (body["event_id"],body["run_id"],"finish",fingerprint(body)))
                    db.execute("UPDATE runs SET state='finishing' WHERE run_id=?", (body["run_id"],))
            # Persist the fixed finish intent before any external read; no new tools
            # can start while source verification is unavailable or after a crash.
            if body["spoke"]:
                error = verify_reply(start, body)
                if error is not None:
                    return error
            finish = {k: v for k, v in body.items() if k != "assistant_reply"}
            with self.db() as db:
                db.execute("UPDATE runs SET state=?,finish_json=? WHERE run_id=?", (body["status"],json.dumps(finish),body["run_id"]))
                row = db.execute("SELECT * FROM runs WHERE run_id=?", (body["run_id"],)).fetchone()
                response = self.response(row, "finish")
                db.execute("UPDATE run_operations SET response_json=? WHERE event_id=?", (json.dumps(response),body["event_id"]))
            self.live.discard(body["run_id"])
            return response

    def query(self, run_id):
        if identity(run_id) != run_id or not isinstance(run_id, str):
            return failure("invalid_identity", "Invalid run_id")
        if not os.path.exists(self.path):
            return failure("run_not_found", "No such run in this instance")
        with self.lock, self.db() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                return failure("run_not_found", "No such run in this instance")
            response = self.response(row, "query")
            response["execution_context_available"] = run_id in self.live and row["state"] == "running"
            response["tool_calls"] = [dict(r) for r in db.execute("SELECT tool_call_id,tool_name,state,outcome_status,outcome_code FROM run_calls WHERE run_id=?",(run_id,))]
            intent = db.execute("SELECT event_id FROM run_operations WHERE run_id=? AND phase='finish'", (run_id,)).fetchone()
            response["finish_event_id"] = intent["event_id"] if intent else None
            return response

    def call(self, run_id, call_id, name, arguments, execute):
        if not isinstance(run_id,str) or identity(run_id) != run_id or not isinstance(call_id,str) or identity(call_id) != call_id:
            return failure("invalid_identity", "Both run and stable tool-call identities are required")
        try:
            digest = fingerprint({"name": name, "arguments": arguments})
        except (TypeError, ValueError):
            return failure("invalid_arguments", "Tool arguments must be finite JSON")
        if not os.path.exists(self.path):
            return failure("run_not_found", "No such run in this instance")
        with self.lock:
            with self.db() as db:
                row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    return failure("run_not_found", "No such run in this instance")
                old = db.execute("SELECT * FROM run_calls WHERE run_id=? AND tool_call_id=?",(run_id,call_id)).fetchone()
                if old is not None:
                    if old["request_hash"] != digest:
                        return failure("id_conflict", "Tool identity content conflict")
                    if old["state"] != "completed":
                        return failure("tool_outcome_unknown", "Do not execute an accepted unknown tool again")
                    cached = self.results.get((run_id,call_id))
                    return json.loads(json.dumps(cached)) if cached is not None else failure("result_not_retained", "Read body was not persisted; do not re-execute this identity")
                if row["state"] != "running":
                    return failure("run_finished", "Run is terminal or finishing; new tools are forbidden")
                if run_id not in self.live:
                    return failure("run_context_lost", "Restarted runs cannot reopen execution; reconcile/finish instead")
                if name not in READ_TOOLS:
                    return failure("run_tool_not_allowed", "Only read_memory_source is available; search_memory has side effects and writes are not supported")
                db.execute("INSERT INTO run_calls VALUES (?,?,?,?,'in_progress',NULL,NULL)",(run_id,call_id,name,digest))
            try:
                result = execute(name, arguments)
                with self.db() as db:
                    db.execute("UPDATE run_calls SET state='completed',outcome_status=?,outcome_code=? WHERE run_id=? AND tool_call_id=?", (result.get("status"),result.get("code"),run_id,call_id))
                self.results[(run_id,call_id)] = json.loads(json.dumps(result))
                while len(self.results) > MAX_READ_RESULTS:
                    self.results.popitem(last=False)
                return result
            except Exception:
                return failure("tool_outcome_unknown", "Accepted tool outcome unknown; reconcile without replay")
