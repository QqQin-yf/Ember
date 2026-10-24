"""Regression tests for actionable MCP parameter diagnostics."""
import copy
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from ember.tool_validation import validate_tool_input
from ember.writer import build_tools_spec


class ToolInputValidationTests(unittest.TestCase):
    def setUp(self):
        self.schema = build_tools_spec()[0]["function"]["parameters"]
        self.args = {
            "event": {
                "summary": "test", "participants": [], "objects": [],
                "context": "", "core": "test", "outcome": "", "state": "",
                "affect": "", "significance": "8: test", "details": [],
                "key_quotes": [],
            },
            "recall_routes": [{"cue_type": "lexical", "cue": "test"}],
            "quote_spans": [{"start_quote": "test", "end_quote": "test"}],
        }

    def test_valid_arguments_unchanged(self):
        before = copy.deepcopy(self.args)
        self.assertIsNone(validate_tool_input(self.args, self.schema))
        self.assertEqual(self.args, before)

    def test_nested_routes_reports_actual_path_without_repairing(self):
        self.args["event"]["recall_routes"] = self.args.pop("recall_routes")
        before = copy.deepcopy(self.args)
        error = validate_tool_input(self.args, self.schema)
        self.assertEqual(error["field"], "recall_routes")
        self.assertEqual(error["actual_path"], "event.recall_routes")
        self.assertIn("平级", error["error"])
        self.assertEqual(self.args, before)

    def test_mcp_sdk_returns_diagnostic_and_never_calls_service(self):
        import mcp.types as types
        import sidecar
        self.args["event"]["recall_routes"] = self.args.pop("recall_routes")
        service = Mock()
        token = sidecar.CURRENT_SERVICE.set(service)
        try:
            request = types.CallToolRequest(method="tools/call",
                params=types.CallToolRequestParams(name="create_memory_event", arguments=self.args))
            with patch.object(sidecar.mcp_server, "_tool_cache", {}):
                # Validation is synchronous inside the async handler. Drive this
                # no-I/O path directly; Windows event loops require socketpair.
                coroutine = sidecar.mcp_server.request_handlers[types.CallToolRequest](request)
                try:
                    with self.assertRaises(StopIteration) as completed:
                        coroutine.send(None)
                    result = completed.exception.value
                finally:
                    coroutine.close()
            receipt = result.root
            self.assertTrue(receipt.isError)
            error = json.loads(receipt.content[0].text)
            self.assertEqual(error["actual_path"], "event.recall_routes")
            service.sidecar_call_tool.assert_not_called()
        finally:
            sidecar.CURRENT_SERVICE.reset(token)

    def test_mcp_marks_business_failures_without_reclassifying_pending_reads(self):
        import sidecar
        service = Mock()
        token = sidecar.CURRENT_SERVICE.set(service)
        try:
            for status, expected in (("failed", True), ("rejected", True), ("created", False),
                                     ("pending", False), ("in_progress", False), ("not_archived", False)):
                with self.subTest(status=status):
                    payload = {"status": status, "code": "test-code"}
                    with patch.object(sidecar.asyncio, "to_thread", new=AsyncMock(return_value=payload)):
                        coroutine = sidecar._call_tool("create_memory_event", self.args)
                        try:
                            with self.assertRaises(StopIteration) as completed:
                                coroutine.send(None)
                            result = completed.exception.value
                        finally:
                            coroutine.close()
                    self.assertEqual(result.isError, expected)
                    self.assertEqual(json.loads(result.content[0].text), payload)
        finally:
            sidecar.CURRENT_SERVICE.reset(token)

    def test_missing_routes(self):
        del self.args["recall_routes"]
        error = validate_tool_input(self.args, self.schema)
        self.assertEqual(error["field"], "recall_routes")
        self.assertNotIn("actual_path", error)

    def test_nested_item_type_error_has_full_path(self):
        self.args["recall_routes"][0]["cue"] = None
        error = validate_tool_input(self.args, self.schema)
        self.assertEqual(error["field"], "recall_routes[0].cue")
        self.assertIn("null", error["error"])

    def test_nested_required_field(self):
        del self.args["event"]["summary"]
        self.assertEqual(validate_tool_input(self.args, self.schema)["field"], "event.summary")

    def test_invalid_enum_and_extra_field_still_rejected(self):
        self.args["recall_routes"][0]["cue_type"] = "invalid"
        self.assertEqual(validate_tool_input(self.args, self.schema)["field"], "recall_routes[0].cue_type")
        self.args["recall_routes"][0]["cue_type"] = "lexical"
        self.args["event"]["extra"] = True
        self.assertIn("extra", validate_tool_input(self.args, self.schema)["error"])


if __name__ == "__main__":
    unittest.main()
