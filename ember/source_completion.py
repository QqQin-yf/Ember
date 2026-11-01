"""Separate, identity-checked turn completion evidence and atomic source views.

Frozen provenance and recall responses are never rewritten. Durable done/tool
receipts are the recovery journal: reconciliation derives the same bindings
after an interrupted finish, without needing a live context or reply text.
"""
from __future__ import annotations

import copy
import json
import os

from .generations import fingerprint
from .source_refs import message_seq


def _version(value):
    return str(value) if value else None


def _project(reference: dict, source: dict | None, bindings: dict) -> dict:
    if (not source or not source.get("identity_verified")
            or reference.get("source_evidence") not in ("frozen_messages", "generation_completed")
            or _version(reference.get("version_id")) != _version(source.get("version_id"))
            or reference.get("source_session_id") != source["source_session_id"]
            or reference.get("source_turn_spans") != source["source_turn_spans"]):
        return copy.deepcopy(reference)
    original = source["turn_messages"]
    messages = copy.deepcopy(original)
    evidence = []
    source_hash = fingerprint(source)
    for item in messages:
        binding = bindings.get((source_hash, item["turn"]))
        if (binding and item.get("user_message_id") == binding["user_message_id"]
                and item.get("assistant_message_id") is None):
            item["assistant_message_id"] = binding["assistant_message_id"]
            item["source_recall_id"] = binding["recall_id"]
            evidence.append(binding)
    # A revision may only enrich the exact frozen provenance, never a newer
    # Event/version, a different path, or arbitrary caller-provided identities.
    if not evidence or reference.get("turn_messages") not in (original, messages):
        return copy.deepcopy(reference)
    result = copy.deepcopy(reference)
    result.update(turn_messages=messages, completion_bindings=copy.deepcopy(evidence),
                  source_evidence="generation_completed", resolution="unresolved",
                  reason="source_seq_missing" if any(not m.get("user_message_id") or
                      not m.get("assistant_message_id") for m in messages) else "source_path_unknown")
    return result


class SourceCompletions:
    def __init__(self, generations, sources):
        self.generations = generations
        self.sources = sources

    @staticmethod
    def _bindings(db) -> dict:
        return {(r["source_hash"], r["turn"]): json.loads(r["binding_json"])
                for r in db.execute("SELECT * FROM source_completions")}

    @staticmethod
    def _verified_binding(db, source: dict) -> dict | None:
        if not source.get("identity_verified"):
            return None
        rid, cid = source.get("writer_recall_id"), source.get("writer_tool_call_id")
        if not rid or not cid:
            return None
        generation = db.execute("SELECT * FROM generations WHERE recall_id=?", (rid,)).fetchone()
        call = db.execute("SELECT * FROM calls WHERE recall_id=? AND tool_call_id=?", (rid, cid)).fetchone()
        snapshot = db.execute("SELECT * FROM recall_snapshots WHERE recall_id=?", (rid,)).fetchone()
        if (not generation or generation["state"] != "done" or not generation["end_hash"]
                or not snapshot or snapshot["state"] != "ready" or not call
                or call["state"] != "completed" or call["tool_name"] not in ("write_event", "write_promise")
                or not call["result_json"]):
            return None
        receipt = json.loads(call["result_json"])
        context = json.loads(snapshot["metadata_json"])["request_context"]
        terminal = json.loads(generation["end_result"])
        user, assistant = message_seq(context.get("user_message_id")), message_seq(generation["assistant_message_id"])
        turn, sid = generation["turn"], generation["session_id"]
        if (receipt.get("status") != "created" or receipt.get("event_id") != source["event_id"]
                or sid != source["source_session_id"] or context.get("session_id") != sid
                or terminal.get("status") != "done" or not terminal.get("ok")
                or terminal.get("session_id") != sid or terminal.get("recall_id") != rid
                or terminal.get("turn") != turn or not user or not assistant
                or not any(lo <= turn <= hi for lo, hi in source["source_turn_spans"])):
            return None
        items = [m for m in source["turn_messages"] if m["turn"] == turn]
        if len(items) != 1 or items[0].get("user_message_id") != user or items[0].get("assistant_message_id") is not None:
            return None
        if source.get("schema_version") != 1 and not any(
                p.get("turn") == turn and p.get("user_message_id") == user
                and p.get("pending_source_recall_id") == rid for p in source.get("pending_sources", [])):
            return None
        return {"turn": turn, "recall_id": rid, "user_message_id": user,
                "assistant_message_id": assistant, "evidence": "turn_end", "status": "done"}

    def _update_views(self, db, records: dict, bindings: dict) -> None:
        for snapshot in db.execute("SELECT recall_id,metadata_json FROM recall_snapshots WHERE state='ready'").fetchall():
            metadata = json.loads(snapshot["metadata_json"])
            frozen = metadata["recall_refs"]["events"]
            row = db.execute("SELECT * FROM recall_sources WHERE recall_id=?", (snapshot["recall_id"],)).fetchone()
            current = json.loads(row["candidates_json"]) if row else frozen
            revised = [_project(ref, records.get(ref["event_id"]), bindings) for ref in current]
            if revised != current:
                db.execute("INSERT INTO recall_sources(recall_id,revision,candidates_json) VALUES (?,?,?) "
                           "ON CONFLICT(recall_id) DO UPDATE SET revision=excluded.revision,candidates_json=excluded.candidates_json",
                           (snapshot["recall_id"], row["revision"] + 1 if row else 1,
                            json.dumps(revised, ensure_ascii=False)))

    def reconcile(self) -> None:
        records = {r["event_id"]: r for r in self.sources.records()}
        if not records or not os.path.exists(self.generations.path):
            return
        with self.generations._db() as db:
            # One SQLite transaction publishes bindings and all changed views.
            # On failure, done receipts remain and are reconciled next time.
            bindings = self._bindings(db)
            for source in records.values():
                binding = self._verified_binding(db, source)
                if binding is None:
                    continue
                key = (fingerprint(source), binding["turn"])
                if key in bindings:
                    if bindings[key] != binding:
                        raise ValueError("Source completion identity conflict")
                    continue
                db.execute("INSERT INTO source_completions(source_hash,turn,binding_json) VALUES (?,?,?)",
                           (*key, json.dumps(binding, ensure_ascii=False)))
                bindings[key] = binding
            self._update_views(db, records, bindings)

    def enrich(self, reference: dict) -> dict:
        if not os.path.exists(self.generations.path):
            return copy.deepcopy(reference)
        with self.generations._db() as db:
            return _project(reference, self.sources.get(reference["event_id"]), self._bindings(db))

    def view(self, recall_id: str, frozen: list[dict]) -> tuple[int, list[dict]]:
        with self.generations._db() as db:
            row = db.execute("SELECT * FROM recall_sources WHERE recall_id=?", (recall_id,)).fetchone()
            return (row["revision"], json.loads(row["candidates_json"])) if row else (0, copy.deepcopy(frozen))
