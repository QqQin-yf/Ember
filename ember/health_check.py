"""Offline diagnostics. Never construct stores or open the original SQLite DB."""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

from .models import Event, RecallRoute


def _json(raw):
    def nonfinite(_):
        raise ValueError("nonfinite")
    return json.loads(raw, parse_constant=nonfinite)


def _positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _message_id(value):
    return _positive(value) or (isinstance(value, str) and value.isascii()
                               and value.isdecimal() and int(value) > 0)


def _spans(value):
    return isinstance(value, list) and bool(value) and all(
        isinstance(pair, list) and len(pair) == 2 and
        all(_positive(n) for n in pair) and pair[0] <= pair[1] for pair in value)


class _Check:
    def __init__(self, root, limit):
        self.root = root
        self.limit = limit
        self.findings = []
        self.counts = Counter()
        self.codes = Counter()
        self.stats = Counter()
        self.files = {}

    def issue(self, severity, code, file, *, line=None, record_id=None):
        self.counts[severity] += 1
        self.codes[code] += 1
        if len(self.findings) < self.limit:
            row = {"severity": severity, "code": code, "file": file}
            if line is not None:
                row["line"] = line
            if record_id is not None:
                row["record_id"] = record_id
            self.findings.append(row)

    def inventory(self):
        result = {}
        for path in self.root.iterdir():
            if path.suffix in (".jsonl", ".sqlite3") or path.name.endswith(("-wal", "-shm", "-journal")) or path.name in (
                    "generations.sqlite3", "generations.sqlite3-wal", "generations.sqlite3-shm", "generations.sqlite3-journal"):
                if path.is_symlink():
                    result[path.name] = ("symlink",)
                else:
                    stat = path.stat()
                    result[path.name] = (stat.st_size, stat.st_mtime_ns)
        return result

    def lifecycle(self):
        """Read only private copies, including committed WAL frames."""
        from contextlib import closing
        for name in ('relationship_timeline.sqlite3','organization.sqlite3','source_sync.sqlite3','extraction.sqlite3'):
            original=self.root/name
            if not original.exists():
                continue
            try:
                if original.is_symlink():
                    self.issue('warning','nonregular_file_skipped',name)
                    continue
                with tempfile.TemporaryDirectory(prefix='ember-lifecycle-health-') as temp:
                    target=Path(temp)/name
                    shutil.copyfile(original,target)
                    for suffix in ('-wal','-journal'):
                        extra=self.root/(name+suffix)
                        if extra.exists() and not extra.is_symlink():
                            shutil.copyfile(extra,Path(str(target)+suffix))
                    with closing(sqlite3.connect(target.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                        if name=='relationship_timeline.sqlite3':
                            row=db.execute("SELECT value FROM metadata WHERE key='health'").fetchone()
                            if row:
                                data=json.loads(row[0])
                                for code in data['codes']:
                                    self.issue('warning',code,name)
                                self.stats['relationship_compressed_and_points_chars']=data['compressed_and_points_chars']
                                self.stats['relationship_original_chars']=data['original_chars']
                        elif name=='organization.sqlite3':
                            count=db.execute('SELECT COUNT(*) FROM processed WHERE error IS NOT NULL').fetchone()[0]
                            self.stats['organization_pending_errors']=count
                            if count:
                                self.issue('warning','organization_maintenance_pending',name)
                        elif name=='extraction.sqlite3':
                            if db.execute("SELECT 1 FROM sqlite_master WHERE name='grouping_retries'").fetchone():
                                rows=db.execute('SELECT slice_id,failures,report_after FROM grouping_retries').fetchall()
                                self.stats['extraction_grouping_pending_errors']=len(rows)
                                repeated=[r for r in rows if r[1]>=r[2]]
                                self.stats['extraction_grouping_repeated_failures']=len(repeated)
                                for row in repeated:
                                    self.issue('warning','extraction_grouping_repeated_failure',name,record_id=row[0])
                        else:
                            count=db.execute('SELECT COUNT(*) FROM sync WHERE error IS NOT NULL').fetchone()[0]
                            if count:
                                self.issue('warning','source_increment_pending',name)
            except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
                self.issue('warning','lifecycle_diagnostics_unavailable',name)

    def rows(self, name):
        path = self.root / name
        if not path.exists():
            return []
        if path.is_symlink() or not path.is_file():
            self.issue("warning", "nonregular_file_skipped", name)
            return []
        rows = []
        try:
            with path.open("rb") as stream:
                for number, raw in enumerate(stream, 1):
                    if not raw.strip():
                        continue
                    try:
                        row = _json(raw.decode("utf-8"))
                        if not isinstance(row, dict):
                            raise ValueError("not object")
                    except (UnicodeError, ValueError):
                        code = "incomplete_jsonl_tail" if not raw.endswith(b"\n") else "invalid_jsonl_record"
                        self.issue("warning" if code == "incomplete_jsonl_tail" else "error", code, name, line=number)
                        continue
                    rows.append((number, row))
        except OSError:
            self.issue("error", "file_unreadable", name)
        self.files[name] = {"parsed_records": len(rows)}
        return rows

    def keyed(self, rows, name, key, *, first=False, model=None):
        result = {}
        for line, row in rows:
            rid = row.get(key)
            if not isinstance(rid, str) or not rid.strip():
                self.issue("error", "invalid_record_id", name, line=line)
                continue
            if model is not None:
                try:
                    model.from_dict(row)
                except (ValueError, TypeError, KeyError):
                    self.issue("error", "invalid_model_record", name, line=line, record_id=rid)
            if rid in result:
                self.stats[name + ":repeated_ids"] += 1
                if first and result[rid][1] != row:
                    self.issue("error", "conflicting_frozen_source", name, line=line, record_id=rid)
            if not first or rid not in result:
                result[rid] = (line, row)
        return result

    def jsonl(self):
        parsed = {name: self.rows(name) for name in sorted(self.inventory()) if name.endswith(".jsonl")}
        events = self.keyed(parsed.get("events.jsonl", []), "events.jsonl", "event_id", model=Event)
        routes = self.keyed(parsed.get("recall_routes.jsonl", []), "recall_routes.jsonl", "route_id", model=RecallRoute)
        sources = self.keyed(parsed.get("event_sources.jsonl", []), "event_sources.jsonl", "event_id", first=True)
        self.stats.update(events=len(events), routes=len(routes), sources=len(sources))
        self.stats["legacy_events_without_frozen_source"] = len(events.keys() - sources.keys())
        for rid, (line, row) in routes.items():
            if not isinstance(row.get("event_id"), str) or row["event_id"] not in events:
                self.issue("error", "route_event_missing", "recall_routes.jsonl", line=line, record_id=rid)
            if not isinstance(row.get("cue"), str) or not row["cue"].strip():
                self.issue("error", "empty_route_cue", "recall_routes.jsonl", line=line, record_id=rid)
        for name in ("turn_spans.jsonl", "pinned.jsonl"):
            records = self.keyed(parsed.get(name, []), name, "event_id")
            for rid, (line, row) in records.items():
                if rid not in events:
                    self.issue("warning", "side_record_event_missing", name, line=line, record_id=rid)
                if name == "turn_spans.jsonl":
                    spans = row.get("spans") if "spans" in row else [[row.get("turn_start"), row.get("turn_end")]]
                    if not _spans(spans):
                        self.issue("error", "invalid_turn_spans", name, line=line, record_id=rid)
        for rid, (line, row) in sources.items():
            if rid not in events:
                # Evidence is committed first: a crash can legitimately leave it orphaned.
                self.issue("warning", "orphan_frozen_source", "event_sources.jsonl", line=line, record_id=rid)
            if not _spans(row.get("source_turn_spans")):
                self.issue("error", "invalid_source_spans", "event_sources.jsonl", line=line, record_id=rid)
            messages = row.get("turn_messages")
            if not isinstance(messages, list) or any(not isinstance(m, dict) or not _positive(m.get("turn")) for m in messages):
                self.issue("error", "invalid_source_messages", "event_sources.jsonl", line=line, record_id=rid)
            if isinstance(messages, list):
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    for field in ("user_message_id", "assistant_message_id"):
                        value = message.get(field)
                        if value is not None and not _message_id(value):
                            self.issue("error", "invalid_source_message_id", "event_sources.jsonl", line=line, record_id=rid)
                    if (_positive(message.get("turn")) and _spans(row.get("source_turn_spans"))
                            and not any(lo <= message["turn"] <= hi for lo, hi in row["source_turn_spans"])):
                        self.issue("error", "source_message_outside_spans", "event_sources.jsonl", line=line, record_id=rid)
            if row.get("identity_verified") is not True:
                self.stats["unverified_frozen_sources"] += 1
            if isinstance(messages, list):
                self.stats["source_turns_with_missing_identity"] += sum(
                    isinstance(m, dict) and (not m.get("user_message_id") or not m.get("assistant_message_id")) for m in messages)
            pending = row.get("pending_sources")
            if isinstance(pending, list):
                self.stats["pending_source_turns"] += len(pending)
        return events

    def sqlite(self, events):
        name = "generations.sqlite3"
        source = self.root / name
        if not source.exists():
            self.stats["generation_database_absent"] = 1
            return
        if source.is_symlink() or not source.is_file():
            self.issue("warning", "nonregular_file_skipped", name)
            return
        journal = self.root / (name + "-journal")
        if journal.exists() and (journal.is_symlink() or journal.stat().st_size > 0):
            # A crash may leave rollback recovery pending. Never ignore its
            # journal and diagnose the un-recovered DB as corruption.
            self.issue("warning", "sqlite_rollback_journal_present", name)
            return
        # SQLite read-only connections can still touch SHM. Only open a private
        # copy, including committed WAL contents; never use immutable=1 on live WAL.
        try:
            with tempfile.TemporaryDirectory(prefix="ember-health-") as temp:
                copied = Path(temp) / name
                shutil.copyfile(source, copied)
                wal = self.root / (name + "-wal")
                if wal.exists():
                    if wal.is_symlink() or not wal.is_file():
                        self.issue("warning", "sqlite_wal_unreadable", name)
                        return
                    shutil.copyfile(wal, Path(temp) / wal.name)
                db = sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)
                try:
                    db.execute("BEGIN")
                    if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                        self.issue("error", "sqlite_integrity_failed", name)
                        return
                    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if not {"generations", "calls", "recall_snapshots"} <= tables:
                        self.issue("warning", "sqlite_schema_incomplete", name)
                        return
                    generations = {r[0] for r in db.execute("SELECT recall_id FROM generations")}
                    self.stats["generations"] = len(generations)
                    for rid, cid, state, volatile, raw in db.execute(
                            "SELECT recall_id,tool_call_id,state,volatile,result_json FROM calls"):
                        self.stats["tool_calls"] += 1
                        if rid not in generations:
                            self.issue("error", "tool_generation_missing", name, record_id=cid)
                        if state in ("unknown", "in_progress"):
                            self.stats["tool_calls_unresolved"] += 1
                        if raw is None:
                            if state == "completed" and not volatile:
                                self.issue("error", "completed_receipt_missing", name, record_id=cid)
                            continue
                        try:
                            result = _json(raw)
                            if not isinstance(result, dict):
                                raise ValueError("not object")
                        except (ValueError, TypeError):
                            self.issue("error", "invalid_tool_receipt", name, record_id=cid)
                            continue
                        if result.get("status") == "created" and (not isinstance(result.get("event_id"), str) or result["event_id"] not in events):
                            self.issue("error", "created_receipt_event_missing", name, record_id=cid)
                    for rid, raw, metadata in db.execute("SELECT recall_id,response_json,metadata_json FROM recall_snapshots"):
                        self.stats["recall_snapshots"] += 1
                        if rid not in generations:
                            self.issue("error", "snapshot_generation_missing", name, record_id=rid)
                        # Null response after TTL is expected, not a damaged snapshot.
                        for value in (raw, metadata):
                            if value is not None:
                                try:
                                    if not isinstance(_json(value), dict):
                                        raise ValueError("not object")
                                except (ValueError, TypeError):
                                    self.issue("error", "invalid_snapshot_json", name, record_id=rid)
                finally:
                    db.close()
        except (OSError, sqlite3.Error):
            self.issue("error", "sqlite_unreadable_or_invalid", name)


def check_memory(data_dir, *, max_findings=200):
    """Read one explicit instance directory; omit all memory/quote/response text."""
    root = Path(data_dir).resolve()
    if max_findings < 1:
        raise ValueError("max_findings must be positive")
    check = _Check(root, max_findings)
    if not root.is_dir():
        check.issue("error", "data_directory_missing", ".")
        initial = None
    else:
        try:
            initial = check.inventory()
            if not ({"events.jsonl", "recall_routes.jsonl", "generations.sqlite3"} & initial.keys()):
                check.issue("warning", "no_memory_store_detected", ".")
            events = check.jsonl()
            check.sqlite(events)
            check.lifecycle()
            if initial != check.inventory():
                check.issue("warning", "data_changed_during_check", ".")
        except OSError:
            check.issue("error", "data_directory_unreadable", ".")
    changed = bool(check.codes["data_changed_during_check"])
    report = {
        "schema_version": 1, "data_dir": str(root), "read_only": True,
        "status": "inconclusive" if changed else "issues" if check.counts else "ok",
        "summary": dict(check.counts), "statistics": dict(check.stats),
        "files": check.files, "findings": check.findings,
        "findings_truncated": sum(check.counts.values()) > len(check.findings),
        "limits": ["No external calls or provenance verification against App/Heart.",
                   "JSONL files are not a transactional snapshot; recheck a quiescent copy if data changes.",
                   "Repeated event/route IDs are append history; missing legacy source IDs are counted, not repaired.",
                   "SQLite is checked through a private DB/WAL copy; expired responses and volatile receipts may be null."]}
    return report
