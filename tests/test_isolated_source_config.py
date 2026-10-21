"""Explicit isolated source config must not inherit or load general credentials."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_phase2_integration.py"
spec = importlib.util.spec_from_file_location("isolated_launcher", SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)

class IsolatedSourceConfigTests(unittest.TestCase):
    def test_default_stays_unconfigured(self):
        self.assertEqual(launcher.source_settings(None, SCRIPT.parents[1]),
                         {"EMBER_SOURCE_URL":"", "EMBER_SOURCE_TOKEN":""})

    def test_only_explicit_source_keys_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp,"source.env")
            path.write_text("EMBER_SOURCE_URL=http://127.0.0.1:8792\nEMBER_SOURCE_TOKEN="+"s"*64+"\nEMBER_SOURCE_TIMEOUT=5\n",encoding="utf-8")
            cfg = launcher.source_settings(path,SCRIPT.parents[1])
            self.assertEqual(cfg["EMBER_SOURCE_TIMEOUT"],"5")
            self.assertEqual(len(cfg["EMBER_SOURCE_TOKEN"]),64)
            self.assertNotIn("DELIZ_TOKEN",cfg)

    def test_wrong_origin_or_keys_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp,"source.env")
            for body in (
                "EMBER_SOURCE_URL=http://remote-host:8792\nEMBER_SOURCE_TOKEN="+"s"*64,
                "EMBER_SOURCE_URL=http://user:pass@127.0.0.1:8792\nEMBER_SOURCE_TOKEN="+"s"*64,
                "EMBER_SOURCE_URL=http://127.0.0.1:8792\nDELIZ_TOKEN=general",
                "EMBER_SOURCE_URL=http://127.0.0.1:8792\nEMBER_SOURCE_TOKEN="+"s"*64+"\nEMBER_SOURCE_TOKEN=duplicate",
                "EMBER_SOURCE_URL=http://127.0.0.1:8792\nEMBER_SOURCE_TOKEN="+"s"*64+"\nEMBER_SOURCE_TIMEOUT=nan"):
                path.write_text(body,encoding="utf-8")
                with self.assertRaises(ValueError):
                    launcher.source_settings(path,SCRIPT.parents[1])

    def test_repository_source_file_rejected_before_read(self):
        with self.assertRaises(ValueError):
            launcher.source_settings(SCRIPT.parents[1]/".env",SCRIPT.parents[1])
