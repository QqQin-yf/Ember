"""Offline health checks preserve source bytes, WAL and legitimate unknown states."""
import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ember.health_check import check_memory, _Check
from ember.models import Event, RecallRoute


class MemoryHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.event = Event(event_id="E", version_id="v", source_ref="old", raw_ref="old",
                           event_time="old", ingest_time="old", summary="PRIVATE SUMMARY", core="PRIVATE CORE").to_dict()
        self.put("events.jsonl", self.event)
        self.put("recall_routes.jsonl", RecallRoute("R", "E", "lexical", "lexical", "PRIVATE CUE").to_dict())

    def put(self, name, *rows):
        (self.root/name).write_text("".join(json.dumps(row,ensure_ascii=False)+"\n" for row in rows),encoding="utf-8")

    def snapshot(self):
        return {p.name:(p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
                for p in self.root.iterdir() if p.is_file()}

    def codes(self, report):
        return {r["code"] for r in report["findings"]}

    def database(self, wal=False):
        db = sqlite3.connect(self.root/"generations.sqlite3")
        if wal:
            db.execute("PRAGMA journal_mode=WAL")
        db.executescript("CREATE TABLE generations(recall_id TEXT);"
                        "CREATE TABLE calls(recall_id TEXT,tool_call_id TEXT,state TEXT,volatile INTEGER,result_json TEXT);"
                        "CREATE TABLE recall_snapshots(recall_id TEXT,response_json TEXT,metadata_json TEXT);")
        db.execute("INSERT INTO generations VALUES ('g')")
        db.commit()
        return db

    def test_healthy_legacy_library_is_read_only_and_private(self):
        before = self.snapshot()
        report = check_memory(self.root)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["statistics"]["legacy_events_without_frozen_source"], 1)
        self.assertEqual(before, self.snapshot())
        raw = json.dumps(report)
        for private in ("PRIVATE SUMMARY", "PRIVATE CORE", "PRIVATE CUE"):
            self.assertNotIn(private, raw)

    def test_repeated_extraction_failures_are_reported_without_reading_source_bodies(self):
        with contextlib.closing(sqlite3.connect(self.root/'extraction.sqlite3')) as db:
            db.execute('CREATE TABLE grouping_retries(slice_id TEXT,failures INTEGER,report_after INTEGER,error TEXT,retry_at REAL)')
            db.executemany('INSERT INTO grouping_retries VALUES (?,?,?,?,?)',[
                ('repeated',3,3,'PRIVATE failure',999999),('initial',1,3,'PRIVATE failure',999999)])
            db.commit()
        before=self.snapshot()
        report=check_memory(self.root)
        self.assertIn('extraction_grouping_repeated_failure',self.codes(report))
        self.assertEqual(report['statistics']['extraction_grouping_pending_errors'],2)
        self.assertEqual(report['statistics']['extraction_grouping_repeated_failures'],1)
        warning=next(r for r in report['findings'] if r['code']=='extraction_grouping_repeated_failure')
        self.assertEqual(warning['record_id'],'repeated')
        self.assertNotIn('PRIVATE failure',json.dumps(report))
        self.assertEqual(before,self.snapshot())

    def test_event_route_append_history_is_legal(self):
        newer = dict(self.event, summary="revised")
        self.put("events.jsonl", self.event, newer)
        self.put("recall_routes.jsonl", {"route_id":"R","event_id":"E","matcher":"lexical","cue_type":"lexical","cue":"old"},
                 {"route_id":"R","event_id":"E","matcher":"lexical","cue_type":"lexical","cue":"new","status":"retired"})
        report=check_memory(self.root)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["statistics"]["events.jsonl:repeated_ids"], 1)

    def test_invalid_json_nonfinite_and_incomplete_tail_are_reported(self):
        (self.root/"extra.jsonl").write_bytes(b'{"x":NaN}\n[1,2]\n{"unfinished":')
        report=check_memory(self.root)
        self.assertIn("invalid_jsonl_record", self.codes(report))
        self.assertIn("incomplete_jsonl_tail", self.codes(report))
        self.assertNotEqual(report["status"],"ok")

    def test_missing_route_target_and_wrong_type_do_not_crash(self):
        route={"route_id":"R","event_id":["missing"],"matcher":"lexical","cue_type":"lexical","cue":" "}
        self.put("recall_routes.jsonl", route)
        report=check_memory(self.root)
        self.assertIn("route_event_missing",self.codes(report))
        self.assertIn("empty_route_cue",self.codes(report))

    def test_pending_and_null_source_identity_are_not_corruption(self):
        self.put("event_sources.jsonl", {"event_id":"E","source_turn_spans":[[1,1]],"identity_verified":False,
                 "turn_messages":[{"turn":1,"user_message_id":"7","assistant_message_id":None}],
                 "pending_sources":[{"turn":1,"pending_source_recall_id":"g"}]})
        report=check_memory(self.root)
        self.assertEqual(report["status"],"ok")
        self.assertEqual(report["statistics"]["pending_source_turns"],1)
        self.assertEqual(report["statistics"]["unverified_frozen_sources"],1)

    def test_invalid_source_identity_and_turn_coverage(self):
        self.put("event_sources.jsonl", {"event_id":"E","source_turn_spans":[[1,1]],"identity_verified":True,
                 "turn_messages":[{"turn":2,"user_message_id":True,"assistant_message_id":"-1"}]})
        codes=self.codes(check_memory(self.root))
        self.assertIn("invalid_source_message_id",codes)
        self.assertIn("source_message_outside_spans",codes)

    def test_orphan_and_conflicting_frozen_source_are_distinct(self):
        row={"event_id":"missing","source_turn_spans":[[1,1]],"turn_messages":[]}
        self.put("event_sources.jsonl", row, dict(row,source_turn_spans=[[2,2]]))
        report=check_memory(self.root)
        self.assertIn("orphan_frozen_source",self.codes(report))
        self.assertIn("conflicting_frozen_source",self.codes(report))

    def test_bad_span_legacy_span_and_pin_reference(self):
        self.put("turn_spans.jsonl", {"event_id":"E","turn_start":1,"turn_end":2})
        self.assertEqual(check_memory(self.root)["status"],"ok")
        self.put("turn_spans.jsonl", {"event_id":"E","spans":[[True,2]]})
        self.put("pinned.jsonl", {"event_id":"absent"})
        codes=self.codes(check_memory(self.root))
        self.assertIn("invalid_turn_spans",codes)
        self.assertIn("side_record_event_missing",codes)

    def test_wal_is_included_without_touching_source_or_recovering_states(self):
        db=self.database(wal=True)
        try:
            db.execute("INSERT INTO calls VALUES ('g','volatile','completed',1,NULL)")
            db.execute("INSERT INTO calls VALUES ('g','unknown','unknown',0,NULL)")
            db.execute("INSERT INTO recall_snapshots VALUES ('g',NULL,'{}')")
            db.commit()
            before=self.snapshot()
            report=check_memory(self.root)
            self.assertEqual(report["status"],"ok",report)
            self.assertEqual(report["statistics"]["generations"],1)
            self.assertEqual(report["statistics"]["tool_calls_unresolved"],1)
            self.assertEqual(report["statistics"]["tool_calls"],2)
            self.assertEqual(before,self.snapshot())
            self.assertEqual(db.execute("SELECT state FROM calls WHERE tool_call_id='unknown'").fetchone()[0],"unknown")
        finally:
            db.close()

    def test_sqlite_receipt_and_generation_links(self):
        db=self.database()
        db.execute("INSERT INTO calls VALUES ('missing','orphan','completed',0,?)",(json.dumps({"status":"created","event_id":"absent"}),))
        db.execute("INSERT INTO calls VALUES ('g','missing-body','completed',0,NULL)")
        db.execute("INSERT INTO recall_snapshots VALUES ('missing','invalid','{}')")
        db.commit();db.close()
        codes=self.codes(check_memory(self.root))
        for code in ("tool_generation_missing","created_receipt_event_missing","completed_receipt_missing","snapshot_generation_missing","invalid_snapshot_json"):
            self.assertIn(code,codes)

    def test_pending_rollback_journal_is_not_recovered_or_called_corruption(self):
        db=self.database();db.close()
        (self.root/"generations.sqlite3-journal").write_bytes(b"pending recovery")
        before=self.snapshot()
        report=check_memory(self.root)
        self.assertIn("sqlite_rollback_journal_present",self.codes(report))
        self.assertNotIn("sqlite_unreadable_or_invalid",self.codes(report))
        self.assertEqual(before,self.snapshot())

    def test_invalid_database_is_reported_without_source_changes(self):
        (self.root/"generations.sqlite3").write_bytes(b"not sqlite PRIVATE TEXT")
        before=self.snapshot()
        report=check_memory(self.root)
        self.assertIn("sqlite_unreadable_or_invalid",self.codes(report))
        self.assertEqual(before,self.snapshot())
        self.assertNotIn("PRIVATE TEXT",json.dumps(report))

    def test_changed_data_remains_inconclusive_when_findings_are_truncated(self):
        original=_Check.inventory
        count=0
        def changed(check):
            nonlocal count
            result=original(check)
            count+=1
            if count >= 3:
                result["new.jsonl"]=(1,1)
            return result
        (self.root/"broken.jsonl").write_bytes(b"bad\n")
        with patch.object(_Check,"inventory",changed):
            report=check_memory(self.root,max_findings=1)
        self.assertEqual(report["status"],"inconclusive")
        self.assertTrue(report["findings_truncated"])

    def test_missing_and_empty_directory_are_not_clean(self):
        self.assertNotEqual(check_memory(self.root/"missing")["status"],"ok")
        empty=self.root/"empty";empty.mkdir()
        self.assertIn("no_memory_store_detected",self.codes(check_memory(empty)))
        self.assertFalse((self.root/"missing").exists())

    def test_cli_json_and_exit_status(self):
        from scripts.check_memory_health import main
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code=main(["--data-dir",str(self.root),"--format","json"])
        self.assertEqual(code,0)
        self.assertEqual(json.loads(output.getvalue())["status"],"ok")
        (self.root/"broken.jsonl").write_bytes(b"bad\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--data-dir",str(self.root)]),1)


if __name__=="__main__":
    unittest.main()
