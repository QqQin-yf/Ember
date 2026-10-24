"""Writer failures retain field paths, remedies and immutable receipts."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from unittest.mock import patch

from ember import source_client
from ember.llm import MockLLMClient
from ember.service import EmberService
from ember.source_paths import QuoteSourceError
from ember.store import EmberStore
from ember.writer import validate_writer_arguments, writer_error_result
from tests.test_sidecar import write_args


class WriterDiagnosticTests(unittest.TestCase):
    def test_semantic_failures_have_exact_paths_and_remedies(self):
        cases = [
            ("recall_routes", [], "recall_routes"),
            ("quote_spans", [], "quote_spans"),
            ("event.summary", " ", "event.summary"),
            ("event.significance", "important", "event.significance"),
            ("event.key_quotes", [{"speaker":"ai","text":"a"}] * 4, "event.key_quotes"),
            ("recall_routes", [{"cue_type":"lexical","cue":" "}], "recall_routes[0].cue"),
            ("quote_spans", [{"start_quote":"a","end_quote":" "}], "quote_spans[0].end_quote"),
        ]
        for field, value, expected in cases:
            with self.subTest(field=expected):
                args = write_args()
                target = args
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                before = copy.deepcopy(args)
                validated, error = validate_writer_arguments(args)
                self.assertIsNone(validated)
                self.assertIsInstance(error, str)  # legacy callers stay compatible
                result = writer_error_result(error)
                self.assertEqual(result["field"], expected)
                self.assertEqual(result["code"], "invalid_tool_arguments")
                self.assertTrue(result["hint"])
                self.assertEqual(args, before)
                self.assertEqual(json.loads(json.dumps(result)), result)

    def test_direct_writer_nested_routes_are_diagnosed_without_moving_them(self):
        args = write_args()
        args["event"]["recall_routes"] = args.pop("recall_routes")
        _, error = validate_writer_arguments(args)
        result = writer_error_result(error)
        self.assertEqual(result["field"], "recall_routes")
        self.assertIn("event.recall_routes", result["hint"])
        self.assertNotIn("recall_routes", args)

    def test_direct_writer_reports_missing_event_field(self):
        args = write_args()
        del args["event"]["summary"]
        _, error = validate_writer_arguments(args)
        self.assertEqual(writer_error_result(error)["field"], "event.summary")

    def test_source_failures_keep_codes_and_distinguish_quote_repair(self):
        for code in ("source_unconfigured", "source_auth_failed", "source_session_unknown",
                     "source_unreachable", "source_identity_missing", "quote_not_found"):
            with self.subTest(code=code):
                result = QuoteSourceError(code).as_result()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["code"], code)
                self.assertTrue(result["hint"])
                self.assertEqual(result.get("field"), "quote_spans" if code == "quote_not_found" else None)

    def test_generation_replays_identical_validation_failure_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = EmberService(EmberStore(tmp), MockLLMClient())
            with patch.object(service, "_fetched_corpus", return_value=[]):
                service.hook_turn_start("s", "actual user text", user_message_id=1, recall_id="r")
                args = write_args(); args["recall_routes"] = []
                first = service.sidecar_call_tool("create_memory_event", args, recall_id="r", tool_call_id="t")
                again = service.sidecar_call_tool("create_memory_event", args, recall_id="r", tool_call_id="t")
            self.assertEqual(first, again)
            self.assertEqual(first["field"], "recall_routes")
            self.assertTrue(first["hint"])
            self.assertFalse(service.store.all_events())
            self.assertEqual(service.sidecar_tool_status("r", "t")["result"], first)

    def test_legacy_acceptance_ignores_host_source_configuration(self):
        from tests import test_sidecar
        with patch.multiple(source_client, SOURCE_URL="http://must-not-contact.invalid", SOURCE_TOKEN="fake"), \
             patch.object(source_client, "_open_source", side_effect=AssertionError("Unexpected network")), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stopped:
            test_sidecar.main()
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(test_sidecar.FAIL, 0)


if __name__ == "__main__":
    unittest.main()
