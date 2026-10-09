# -*- coding: utf-8 -*-
"""turn_span 可断开区间列表测试（架构文档 §4.1，2026-10-08 定稿）。

覆盖：
- fill 区间列表 + 规范化（排序/合并/拒绝逆序/拒绝空）
- v1 单区间旧数据兼容读入
- span_of 降级（锚点 min/max / 无出处返回空列表）
- covers 交集判断（候选组装用）
- 文件行格式 v2
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.turn_span import TurnSpanStore, _norm_spans


def test_fill_and_reload():
    with tempfile.TemporaryDirectory() as root:
        ts = TurnSpanStore(root)
        ts.fill("EVT-001", [(1, 10), (15, 20)])
        ts.fill("EVT-002", [(3, 3)])
        ts2 = TurnSpanStore(root)
        assert ts2.span_of("EVT-001") == [(1, 10), (15, 20)]
        assert ts2.span_of("EVT-002") == [(3, 3)]
        assert ts2.provenance["EVT-001"] == "sleep-closing"
        print("ok  test_fill_and_reload")


def test_norm():
    assert _norm_spans([(15, 20), (1, 10)]) == [(1, 10), (15, 20)]  # 排序
    assert _norm_spans([(1, 10), (5, 12)]) == [(1, 12)]              # 重叠合并
    assert _norm_spans([(1, 10), (11, 20)]) == [(1, 20)]             # 相邻合并
    assert _norm_spans([(1, 10), (12, 20)]) == [(1, 10), (12, 20)]   # 断开保留
    try:
        _norm_spans([(10, 1)])
        assert False, "应拒绝逆序区间"
    except ValueError:
        pass
    print("ok  test_norm")


def test_v1_compat():
    with tempfile.TemporaryDirectory() as root:
        p = os.path.join(root, "turn_spans.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write(json.dumps({"event_id": "EVT-OLD", "turn_start": 2,
                                "turn_end": 7, "filled_by": "legacy"}) + "\n")
        ts = TurnSpanStore(root)
        assert ts.span_of("EVT-OLD") == [(2, 7)]
        assert ts.provenance["EVT-OLD"] == "legacy"
        print("ok  test_v1_compat")


def test_degrade():
    with tempfile.TemporaryDirectory() as root:
        ts = TurnSpanStore(root)
        # 无回填有锚点 → min/max 单区间
        assert ts.span_of("EVT-X", anchor_turns=[4, 2, 9]) == [(2, 9)]
        # 无回填无锚点 → 空列表（未知出处）
        assert ts.span_of("EVT-Y") == []
        # fill 空 spans 拒绝
        try:
            ts.fill("EVT-Z", [])
            assert False, "应拒绝空区间列表"
        except ValueError:
            pass
        print("ok  test_degrade")


def test_covers():
    with tempfile.TemporaryDirectory() as root:
        ts = TurnSpanStore(root)
        ts.fill("EVT-001", [(1, 10), (15, 20)])
        assert ts.covers("EVT-001", 5)
        assert ts.covers("EVT-001", 17)
        assert not ts.covers("EVT-001", 12)   # 断开区间之外
        assert not ts.covers("EVT-001", 21)
        assert not ts.covers("EVT-404", 5)
        print("ok  test_covers")


def test_file_format_v2():
    with tempfile.TemporaryDirectory() as root:
        ts = TurnSpanStore(root)
        ts.fill("EVT-001", [(1, 10), (15, 20)])
        p = os.path.join(root, "turn_spans.jsonl")
        with open(p, encoding="utf-8") as f:
            line = json.loads(f.readline())
        assert line["spans"] == [[1, 10], [15, 20]]
        assert {"event_id", "spans", "filled_by", "recorded_at"} <= set(line)
        print("ok  test_file_format_v2")


if __name__ == "__main__":
    test_fill_and_reload()
    test_norm()
    test_v1_compat()
    test_degrade()
    test_covers()
    test_file_format_v2()
    print("turn_span tests: all passed")
