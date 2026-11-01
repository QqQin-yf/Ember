"""Quote coordinates tolerate presentation differences, never missing words."""
import tempfile
import unittest
from unittest.mock import patch

from ember.llm import MockLLMClient
from ember.service import EmberService
from ember.source_paths import QuoteSourceError
from ember.store import EmberStore
from ember.turn_span import _locate_index, spans_from_quotes
from tests.test_sidecar import write_args as _write_args

def write_args(*args,**kwargs):
    value=_write_args(*args,**kwargs); value["event"]["focus"]="1"; return value



def rows(*texts):
    return [dict(role="user", turn=i+1, content=text) for i, text in enumerate(texts)]


class QuoteMatchingTests(unittest.TestCase):
    def test_omitted_trailing_emoji_is_already_an_exact_substring(self):
        self.assertEqual(_locate_index("我想去", rows("我想去🥰")), 0)

    def test_exact_match_has_priority_over_formatting_candidates(self):
        self.assertEqual(_locate_index("我想去", rows("**我**想**去**", "我想去🥰")), 1)

    def test_formatting_in_either_source_or_quote(self):
        for quote, source in [
            ("我想去", "**我***想***去**🥰"),
            ("**我**想**去**", "我想去"),
            ("我想去。", "我🥰想\n去！"),
            ("我想去🥰", "我想去"),
            ("我 想 去", "我\t想\n去"),
            ("预算2026元", "预算：**2026**元💰"),
        ]:
            with self.subTest(quote=quote, source=source):
                corpus = rows(source)
                self.assertEqual(_locate_index(quote, corpus), 0)
                self.assertEqual(corpus[0]["content"], source)

    def test_no_edit_distance_or_subsequence_matching(self):
        for source in ("我不想去", "我想要去", "我想来", "预算2027元"):
            quote = "预算2026元" if source.startswith("预算") else "我想去"
            self.assertIsNone(_locate_index(quote, rows(source)))

    def test_symbol_only_quote_does_not_match_everything(self):
        self.assertIsNone(_locate_index("🥰***", rows("任何文字")))

    def test_ambiguous_fallback_reports_exact_endpoint(self):
        for endpoint in ("start_quote", "end_quote"):
            pair = dict(start_quote="独特开头", end_quote="独特结尾")
            pair[endpoint] = "我想去"
            with self.assertRaises(QuoteSourceError) as caught:
                spans_from_quotes([pair], rows("独特开头", "**我**想去", "我想**去**", "独特结尾"))
            result = caught.exception.as_result()
            self.assertEqual(result["code"], "quote_ambiguous")
            self.assertEqual(result["field"], "quote_spans[0]."+endpoint)
            self.assertEqual(result["candidate_count"], 2)
            self.assertIn("扩长", result["hint"])

    def test_repetitions_in_one_message_have_one_coordinate(self):
        self.assertEqual(_locate_index("我想去", rows("**我**想去；**我**想去")), 0)

    def test_fallback_keeps_actual_turn_coordinates(self):
        corpus = rows("开头", "**我**想**去**", "中间", "结尾🥰")
        self.assertEqual(spans_from_quotes([dict(start_quote="我想去", end_quote="结尾🥰🥰")], corpus), [(2, 4)])

    def test_ambiguous_write_rejected_before_any_event_or_source_capture(self):
        with tempfile.TemporaryDirectory() as root:
            service = EmberService(EmberStore(root), MockLLMClient())
            with patch.object(service, "_fetched_corpus", return_value=[]):
                service.hook_turn_start("quote-test", "真实合成消息", user_message_id=1)
            args = write_args(quote_spans=[dict(start_quote="我想去", end_quote="独特结尾")])
            with patch.object(service, "_quote_corpus", return_value=rows("**我**想去", "我想**去**", "独特结尾")), \
                 patch.object(service.event_sources, "capture") as capture:
                result = service.sidecar_create_memory_event(args)
            self.assertEqual(result["code"], "quote_ambiguous")
            self.assertEqual(result["field"], "quote_spans[0].start_quote")
            self.assertFalse(service.store.all_events())
            capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()
