import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from ember import source_client
from ember.turn_slices import TurnSliceStore, fetch_and_save_turn


class TurnSliceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = TurnSliceStore(self.tmp.name)
        self.pair = [{"message_id": 71, "role": "user", "content": "她的原话\n保留换行"},
                     {"message_id": 75, "role": "assistant", "content": "他的原话🙂"}]

    def test_original_pair_survives_reopen_and_retry(self):
        saved = self.store.save("session", self.pair)
        self.assertEqual([m["content"] for m in saved["messages"]],
                         [m["content"] for m in self.pair])
        self.assertEqual(TurnSliceStore(self.tmp.name).get(saved["slice_id"]), saved)
        self.assertEqual(self.store.save("session", self.pair), saved)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM turn_slices").fetchone()[0], 1)

    def test_system_side_is_removed_before_persistence(self):
        for marker in ("flag", "role", "provider"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as tmp:
                pair = [dict(m) for m in self.pair]
                if marker == "role":
                    pair[0]["role"] = "system"
                if marker == "provider":
                    pair[0]["is_system"] = True
                store = TurnSliceStore(tmp)
                saved = store.save("s", pair, user_is_system=marker == "flag")
                self.assertEqual([m["role"] for m in saved["messages"]], ["assistant"])
                self.assertTrue(saved["user_is_system"])
                self.assertNotIn(pair[0]["content"], json.dumps(saved, ensure_ascii=False))
                self.assertNotIn(pair[0]["content"].encode(), store.path.read_bytes())

    def test_system_word_in_human_message_is_preserved(self):
        self.pair[0]["content"] = "系统消息该怎么处理？<system-reminder>是一个标签"
        saved = self.store.save("s", self.pair)
        self.assertEqual(len(saved["messages"]), 2)

    def test_correcting_system_flag_removes_previously_saved_text(self):
        saved = self.store.save("s", self.pair)
        corrected = self.store.save("s", self.pair, user_is_system=True)
        self.assertEqual(corrected["slice_id"], saved["slice_id"])
        self.assertEqual(len(corrected["messages"]), 1)
        self.assertNotIn(self.pair[0]["content"].encode(), self.store.path.read_bytes())

    def test_assistant_only_turn(self):
        saved = self.store.save("s", self.pair[1:])
        self.assertEqual(saved["messages"][0]["content"], self.pair[1]["content"])
        self.assertIsNone(saved["user_message_id"])

    def test_instance_session_and_regeneration_are_distinct(self):
        a = self.store.save("s", self.pair)
        b = self.store.save("s", self.pair, instance="second")
        c = self.store.save("other", self.pair)
        self.pair[1]["message_id"] = 76
        d = self.store.save("s", self.pair)
        self.assertEqual(len({row["slice_id"] for row in (a, b, c, d)}), 4)

    def test_refetch_updates_same_copy(self):
        saved = self.store.save("s", self.pair)
        self.pair[1]["content"] = "revised original"
        updated = self.store.save("s", self.pair)
        self.assertEqual(updated["slice_id"], saved["slice_id"])
        self.assertNotEqual(updated["content_hash"], saved["content_hash"])

    def test_fetch_then_save_exact_path_and_system_exclusion(self):
        with patch.object(source_client, "fetch_source_path", return_value=self.pair) as fetch:
            saved = fetch_and_save_turn(self.store, "s", 75, user_message_id=71,
                                        instance="second", user_is_system=True)
        fetch.assert_called_once_with("s", ["71", "75"], instance="second")
        self.assertEqual([m["role"] for m in saved["messages"]], ["assistant"])

    def test_source_failure_and_wrong_pair_do_not_write(self):
        for result in ([], self.pair[:1], list(reversed(self.pair))):
            with patch.object(source_client, "fetch_source_path", return_value=result):
                with self.assertRaises(source_client.SourceUnavailable):
                    fetch_and_save_turn(self.store, "s", 75, user_message_id=71)
        with patch.object(source_client, "fetch_source_path",
                          side_effect=source_client.SourceUnavailable("offline")):
            with self.assertRaises(source_client.SourceUnavailable):
                fetch_and_save_turn(self.store, "s", 75, user_message_id=71)
        self.assertFalse(self.store.path.exists())

    def test_incomplete_or_wrong_roles_rejected(self):
        for pair in ([], self.pair[:1], list(reversed(self.pair)),
                     [self.pair[1], self.pair[1]], self.pair * 2):
            with self.assertRaises(ValueError):
                self.store.save("s", pair)
        self.assertFalse(self.store.path.exists())
