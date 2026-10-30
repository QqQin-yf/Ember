from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import contextlib
import runpy
import sys
from unittest.mock import patch

from ember.slice_embedding import (SliceEmbeddingConfig, SliceEmbeddingClient,
    SliceEmbeddingError, embed_pending_slices, slice_text, DEFAULT_MODEL)
from ember.turn_slices import TurnSliceStore


class SliceEmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = TurnSliceStore(self.tmp.name)
        self.config = SliceEmbeddingConfig("synthetic-secret")
        self.client = SliceEmbeddingClient(self.config)
        self.pair = [{"message_id": 1, "role": "user", "content": "用户原话\n保留换行"},
                     {"message_id": 2, "role": "assistant", "content": "助手原话🙂"}]

    def response(self, data=None):
        if data is None:
            data = {"model": DEFAULT_MODEL,
                    "data": [{"index": 0, "embedding": [0.25, 0.5, -0.75]}]}
        return io.BytesIO(json.dumps(data).encode())

    def test_config_and_key_are_private(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"api_key": "synthetic-secret"}), encoding="utf-8-sig")
        config = SliceEmbeddingConfig.load(path)
        self.assertEqual(config.model, DEFAULT_MODEL)
        self.assertNotIn("synthetic-secret", repr(config))
        self.assertNotIn("synthetic-secret", config.space)

    def test_missing_and_invalid_config_fail_without_secret(self):
        path = self.root / "config.json"
        with self.assertRaisesRegex(SliceEmbeddingError, "config_not_found"):
            SliceEmbeddingConfig.load(path)
        for data in ({}, {"api_key": ""}, {"api_key": "synthetic-secret", "timeout_seconds": -1},
                     {"api_key": "synthetic-secret", "base_url": "http://example.com/v1"},
                     {"api_key": "synthetic-secret", "base_url": "https://[invalid"},
                     {"api_key": "synthetic-secret", "base_url": "https://u:p@example.com/v1"}):
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(SliceEmbeddingError) as error:
                SliceEmbeddingConfig.load(path)
            self.assertNotIn("synthetic-secret", str(error.exception))

    def test_one_whole_slice_is_sent_as_one_input_string(self):
        source = self.store.save("s", self.pair)
        with patch("urllib.request.urlopen", return_value=self.response()) as send:
            counts = embed_pending_slices(self.store, self.client)
        request = send.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.siliconflow.cn/v1/embeddings")
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-secret")
        self.assertEqual(payload, {"model": DEFAULT_MODEL, "encoding_format": "float",
            "input": "user:\n用户原话\n保留换行\n\nassistant:\n助手原话🙂"})
        self.assertEqual(counts, {"selected": 1, "ready": 1, "failed": 0, "changed": 0})
        reopened = TurnSliceStore(self.tmp.name).embedding(source["slice_id"], self.config.space)
        self.assertEqual(reopened["vector"], [0.25, 0.5, -0.75])
        self.assertEqual(reopened["dimensions"], 3)

    def test_system_side_never_enters_embedding_request(self):
        source = self.store.save("s", self.pair, user_is_system=True)
        self.assertEqual(slice_text(source), "assistant:\n助手原话🙂")

    def test_ready_vectors_are_skipped(self):
        self.store.save("s", self.pair)
        with patch.object(self.client, "embed", return_value=[1.0, 0.5]) as embed:
            embed_pending_slices(self.store, self.client)
            counts = embed_pending_slices(self.store, self.client)
        self.assertEqual(embed.call_count, 1)
        self.assertEqual(counts["selected"], 0)

    def test_changed_source_invalidates_and_recomputes_vector(self):
        source = self.store.save("s", self.pair)
        with patch.object(self.client, "embed", return_value=[1.0, 0.5]):
            embed_pending_slices(self.store, self.client)
        self.pair[1]["content"] = "更新后的原文"
        self.store.save("s", self.pair)
        self.assertIsNone(self.store.embedding(source["slice_id"], self.config.space))
        with patch.object(self.client, "embed", return_value=[0.5, 1.0]) as embed:
            embed_pending_slices(self.store, self.client)
        self.assertIn("更新后的原文", embed.call_args.args[0])

    def test_provider_or_model_change_uses_separate_space(self):
        source = self.store.save("s", self.pair)
        with patch.object(self.client, "embed", return_value=[1.0, 0.5]):
            embed_pending_slices(self.store, self.client)
        for config in (replace(self.config, model="different-model"),
                       replace(self.config, base_url="https://different-provider.example/v1")):
            self.assertEqual(len(self.store.pending_embeddings(config.space)), 1)
            self.assertIsNone(self.store.embedding(source["slice_id"], config.space))

    def test_failed_request_preserves_source_and_can_retry(self):
        source = self.store.save("s", self.pair)
        with patch.object(self.client, "embed", side_effect=SliceEmbeddingError("embedding_http_429")):
            counts = embed_pending_slices(self.store, self.client)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(self.store.get(source["slice_id"]), source)
        self.assertIsNone(self.store.embedding(source["slice_id"], self.config.space))
        with patch.object(self.client, "embed", return_value=[1.0, 0.5]):
            self.assertEqual(embed_pending_slices(self.store, self.client)["ready"], 1)

    def test_changed_during_request_does_not_store_stale_vector(self):
        source = self.store.save("s", self.pair)
        def change(text):
            self.pair[1]["content"] = "changed while API was running"
            self.store.save("s", self.pair)
            return [1.0, 0.5]
        with patch.object(self.client, "embed", side_effect=change):
            self.assertEqual(embed_pending_slices(self.store, self.client)["changed"], 1)
        self.assertIsNone(self.store.embedding(source["slice_id"], self.config.space))

    def test_invalid_embeddings_are_never_marked_ready(self):
        self.store.save("s", self.pair)
        vectors = ([], [0, 0], [float("nan")], [float("inf")], ["1"], [True])
        for vector in vectors:
            with self.subTest(vector=vector), patch("urllib.request.urlopen", return_value=self.response(
                    {"data": [{"index": 0, "embedding": vector}]})):
                self.assertEqual(embed_pending_slices(self.store, self.client)["failed"], 1)
        self.assertEqual(len(self.store.pending_embeddings(self.config.space)), 1)

    def test_bad_response_identity_and_shape_rejected(self):
        for data in ({}, {"data": []}, {"data": [{"index": 2, "embedding": [1]}]},
                     {"model": "wrong", "data": [{"index": 0, "embedding": [1]}]}):
            with self.subTest(data=data), patch("urllib.request.urlopen", return_value=self.response(data)):
                with self.assertRaisesRegex(SliceEmbeddingError, "embedding_response_invalid"):
                    self.client.embed("text")

    def test_http_error_and_provider_body_do_not_leak_secrets(self):
        error = urllib.error.HTTPError("https://example.com", 401, "synthetic-secret", {},
                                      io.BytesIO(b'synthetic-secret private-source-text'))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(SliceEmbeddingError, "^embedding_http_401$"):
                self.client.embed("private-source-text")
        self.assertTrue(error.fp.closed)

    def test_network_error_is_safe(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("synthetic-secret")):
            with self.assertRaisesRegex(SliceEmbeddingError, "^embedding_unreachable$"):
                self.client.embed("text")

    def test_no_database_is_an_empty_queue(self):
        with patch.object(self.client, "embed") as embed:
            self.assertEqual(embed_pending_slices(self.store, self.client)["selected"], 0)
        embed.assert_not_called()

    def test_cli_reads_config_writes_vectors_and_reports_without_private_text(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"api_key": "synthetic-secret"}), encoding="utf-8")
        source = self.store.save("s", self.pair)
        script = Path(__file__).resolve().parents[1] / "scripts/embed_turn_slices.py"
        output = io.StringIO()
        argv = [str(script), "--config", str(path), "--data-dir", str(self.root)]
        with patch.object(sys, "argv", argv), contextlib.redirect_stdout(output), \
                patch("urllib.request.urlopen", return_value=self.response()):
            self.assertEqual(runpy.run_path(str(script))["main"](), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["ready"], 1)
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertNotIn(self.pair[0]["content"], output.getvalue())
        self.assertIsNotNone(TurnSliceStore(str(self.root)).embedding(source["slice_id"], self.config.space))

    def test_cli_missing_key_does_not_make_request(self):
        path = self.root / "config.json"
        path.write_text('{"api_key":""}', encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts/embed_turn_slices.py"
        output = io.StringIO()
        with patch.object(sys, "argv", [str(script), "--config", str(path)]), \
                contextlib.redirect_stdout(output), patch("urllib.request.urlopen") as request:
            self.assertEqual(runpy.run_path(str(script))["main"](), 1)
        request.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["code"], "api_key_missing")
