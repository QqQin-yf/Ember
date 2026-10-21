"""Trust exact generation/provenance identities, not legacy max-reply mappings."""
from __future__ import annotations
import json
import os
from . import source_client
from .source_refs import message_seq


def generation_messages(generations, sid: str, *, completed_only=False) -> list[dict]:
    if not os.path.exists(generations.path):
        return []
    with generations._db() as db:
        rows = db.execute("SELECT g.*,s.metadata_json FROM generations g JOIN recall_snapshots s "
                          "USING(recall_id) WHERE g.session_id=? AND s.state='ready' "
                          "ORDER BY g.turn", (sid,)).fetchall()
    messages = []
    for row in rows:
        if completed_only and row["state"] != "done":
            continue
        context = json.loads(row["metadata_json"])["request_context"]
        if context.get("session_id") != sid:
            continue
        user = message_seq(context.get("user_message_id"))
        assistant = message_seq(row["assistant_message_id"]) if row["state"] == "done" else None
        if user:
            messages.append({"turn": row["turn"], "role": "user", "message_id": user,
                             "recall_id": row["recall_id"]})
        if assistant:
            messages.append({"turn": row["turn"], "role": "assistant", "message_id": assistant,
                             "recall_id": row["recall_id"]})
    return messages


def read_messages(sid: str, identities: list[dict], instance: str) -> list[dict]:
    if not identities:
        raise source_client.SourceUnavailable("Source identity evidence missing")
    by_seq = {}
    for item in identities:
        seq = message_seq(item.get("message_id"))
        if seq is None or seq in by_seq:
            raise source_client.SourceUnavailable("Source identity ambiguous")
        by_seq[seq] = item
    rows = source_client.fetch_source_path(sid, list(by_seq), instance=instance)
    out = []
    # Check even injected/mock clients: no partial response or unexpected rows.
    if len(rows) != len(by_seq):
        raise source_client.SourceUnavailable("Source identities incomplete")
    seen = set()
    for row in rows:
        seq = message_seq(row.get("message_id"))
        identity = by_seq.get(seq)
        if (not identity or seq in seen or identity["role"] != row.get("role")
                or not isinstance(row.get("content"), str)):
            raise source_client.SourceUnavailable("Source identity mismatch")
        seen.add(seq)
        out.append(dict(row, turn=identity["turn"], _source_session_id=sid,
                        _source_message_id=seq, _source_recall_id=identity.get("recall_id")))
    return out
