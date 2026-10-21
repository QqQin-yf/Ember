"""Frozen event provenance, separate from Event and legacy turn mappings.

Only message identities carried by the exact quote corpus are evidence. The
legacy numeric range endpoint and max-reply ledger cannot establish identity.
No message text, injected text or recall response is persisted here.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Any

SOURCE_REFS_FILE = "event_sources.jsonl"


def message_seq(value: Any) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return str(int(value)) if int(value) > 0 else None
    return None


class EventSourceStore:
    """First provenance record wins, including explicitly missing identities."""

    def __init__(self, root: str):
        self.path = os.path.join(root, SOURCE_REFS_FILE)
        self._records: dict[str, dict] = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        row = json.loads(line)
                        self._records.setdefault(row["event_id"], row)

    def get(self, event_id: str) -> dict | None:
        return copy.deepcopy(self._records.get(event_id))

    def capture(self, event_id: str, version_id: str | None,
                session_id: str, spans: list[tuple[int, int]],
                corpus: list[dict], writer_recall_id: str | None = None,
                writer_tool_call_id: str | None = None) -> None:
        if event_id in self._records:
            return
        turns = sorted({turn for lo, hi in spans
                        for turn in range(lo, hi + 1)})
        messages = []
        identity_verified = True
        for turn in turns:
            item: dict[str, Any] = {"turn": turn}
            for role, field in (("user", "user_message_id"),
                                ("assistant", "assistant_message_id")):
                rows = [m for m in corpus if m.get("turn") == turn
                        and m.get("role") == role]
                # Every row used for this role/turn must carry exact identity.
                # Fetched legacy rows deliberately have no binding metadata.
                ids = {message_seq(m.get("_source_message_id"))
                       if m.get("_source_session_id") == session_id else None
                       for m in rows}
                if rows and (None in ids or len(ids) != 1):
                    identity_verified = False
                item[field] = next(iter(ids)) if len(ids) == 1 else None
            assistant_rows = [m for m in corpus if m.get("turn") == turn
                              and m.get("role") == "assistant"]
            source_generations = {m.get("_source_recall_id") for m in assistant_rows}
            if (item["assistant_message_id"] and len(source_generations) == 1
                    and None not in source_generations):
                item["source_recall_id"] = next(iter(source_generations))
            messages.append(item)
        row = {"schema_version": 1, "event_id": event_id,
               "version_id": version_id, "source_session_id": session_id,
               "source_turn_spans": [list(span) for span in spans],
               "turn_messages": messages, "identity_verified": identity_verified}
        if writer_recall_id is not None:
            row["writer_recall_id"] = writer_recall_id
            row["writer_tool_call_id"] = writer_tool_call_id
        # Commit evidence before Event: an interrupted write may leave an orphan
        # record, but cannot create an Event whose evidence was never captured.
        with open(self.path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._records[event_id] = copy.deepcopy(row)


def event_reference(event, presentation: str, sources: EventSourceStore,
                    legacy_spans: list[tuple[int, int]]) -> dict:
    frozen = sources.get(event.event_id)
    sid = None
    if (event.source_ref or "").startswith("conversation/session-"):
        sid = event.source_ref[len("conversation/session-"):].split("/", 1)[0] or None
    spans = [list(span) for span in legacy_spans]
    messages = []
    evidence = "legacy_unverified"
    if frozen is not None:
        sid = frozen["source_session_id"]
        spans = frozen["source_turn_spans"]
        messages = frozen["turn_messages"]
        evidence = "frozen_messages"
        if not frozen.get("identity_verified"):
            # app's endpoint resolver can bridge missing middle turns. Do not
            # let valid endpoints launder unverified legacy corpus identities.
            messages = []
            evidence = "unverified_corpus"
    if not sid:
        reason = "source_session_unknown"
    elif not spans:
        reason = "source_span_missing"
    elif frozen is None or evidence == "unverified_corpus":
        reason = "source_path_unknown"
    elif any(not m.get("user_message_id") or not m.get("assistant_message_id")
             for m in messages):
        reason = "source_seq_missing"
    else:
        # Exact IDs are not proof of a parent path. Only app can resolve it.
        reason = "source_path_unknown"
    return {"event_id": event.event_id,
            "version_id": str(event.version_id) if event.version_id else None,
            "presentation": presentation, "source_session_id": sid,
            "source_turn_spans": spans, "turn_messages": messages,
            "source_evidence": evidence, "resolution": "unresolved",
            "reason": reason, "source_leaf_id": None, "message_ids": None}
