"""Deterministic process death inside actual source publication transactions."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]

class SourceProcessCrashTests(unittest.TestCase):
    def run_child(self, action, root, checkpoint):
        return subprocess.run([sys.executable, "-B", "-m", "tests.source_crash_worker", action,
                               root, checkpoint], cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", timeout=40, check=True)

    def test_kill_uncommitted_and_committed_publication_recovers_once(self):
        for checkpoint in ("binding_uncommitted", "first_view_uncommitted", "views_uncommitted", "committed_before_ack"):
            with self.subTest(checkpoint=checkpoint), tempfile.TemporaryDirectory() as root:
                self.run_child("prepare", root, checkpoint)
                path = Path(root, "generations.sqlite3")
                with closing(sqlite3.connect(path)) as db:
                    frozen = db.execute("SELECT recall_id,metadata_json,response_json FROM recall_snapshots ORDER BY recall_id").fetchall()
                    receipts = db.execute("SELECT * FROM calls").fetchall()
                events = Path(root, "events.jsonl").read_bytes()
                sources = Path(root, "event_sources.jsonl").read_bytes()
                child = subprocess.Popen([sys.executable, "-B", "-m", "tests.source_crash_worker", "kill",
                                          root, checkpoint], cwd=ROOT, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 25
                    marker = Path(root, "checkpoint")
                    while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.025)
                    self.assertTrue(marker.exists(), "child did not reach " + checkpoint)
                    self.assertIsNone(child.poll())
                    self.assertEqual(marker.read_text(encoding="utf-8"), checkpoint)
                    child.kill()  # TerminateProcess on Windows; no Python rollback/finally.
                    child.wait(timeout=10)
                    self.assertNotEqual(child.returncode, 0)
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.communicate(timeout=10)
                # Read before service startup reconciliation: neither a half binding
                # nor a half view may be externally visible after recovery by SQLite.
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(db.execute("SELECT state FROM generations WHERE recall_id='writer'").fetchone()[0], "done")
                    committed = checkpoint == "committed_before_ack"
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM source_completions").fetchone()[0], int(committed))
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM recall_sources").fetchone()[0], 2 * int(committed))
                for _ in range(2):
                    result = self.run_child("recover", root, checkpoint)
                    self.assertEqual(json.loads(result.stdout)["revision"], 1)
                with closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute("SELECT recall_id,metadata_json,response_json FROM recall_snapshots ORDER BY recall_id").fetchall(), frozen)
                    self.assertEqual(db.execute("SELECT * FROM calls").fetchall(), receipts)
                    self.assertEqual(db.execute("SELECT revision FROM recall_sources").fetchall(), [(1,), (1,)])
                self.assertEqual(Path(root, "events.jsonl").read_bytes(), events)
                self.assertEqual(Path(root, "event_sources.jsonl").read_bytes(), sources)
