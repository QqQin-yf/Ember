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
        # 未回填 → []（无真实坐标，诚实上报——2026-10-12 裁决移除锚点代答）
        assert ts.span_of("EVT-X") == []
        # span_of_event 同语义：turn_refs 只是出生图章，不再代答坐标
        ev = type("E", (), {"event_id": "EVT-X",
                            "turn_refs": ["turn-4"]})()
        assert ts.span_of_event(ev) == []
        # 回填后 → 精确区间
        ts.fill("EVT-X", [(2, 9)])
        assert ts.span_of("EVT-X") == [(2, 9)]
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


def test_spans_from_quotes_half_valid():
    # 2026-10-12 裁决：半有效句对 → 缺失端取就近边界（2026-10-17 momo
    # 改定：单端定位 ±10 条消息，不再拉到会话尽头）；双盲 → 整对放弃。
    # 语料=持久存档∪内存（见 service._quote_corpus）。本用例语料只有
    # 3 条消息，就近边界与旧保证边界结果相同（都到语料边缘）。
    from ember.turn_span import spans_from_quotes
    msgs = [
        {"role": "user", "turn": 1,
         "content": "今天我们决定把 Ember 的传播模型改成 Route Search。"},
        {"role": "assistant", "turn": 2, "content": "好的，记下了。"},
        {"role": "user", "turn": 3, "content": "那梦通道也一起对一下吧。"},
    ]
    # 起句有效、尾句编造 → 从首句往后 10 条消息（不足到末尾）
    r = spans_from_quotes(
        [{"start_quote": "今天我们决定把", "end_quote": "这句话不存在"}], msgs)
    assert r == [(1, 3)], r
    # 起句编造、尾句有效 → 从尾句往前 10 条消息（不足到开头）
    r = spans_from_quotes(
        [{"start_quote": "这句话不存在", "end_quote": "梦通道"}], msgs)
    assert r == [(1, 3)], r
    # 双盲 → 整对放弃
    r = spans_from_quotes(
        [{"start_quote": "不存在甲", "end_quote": "不存在乙"}], msgs)
    assert r == [], r
    # 全有效 → 精确区间（跨轮）
    r = spans_from_quotes(
        [{"start_quote": "今天我们决定把", "end_quote": "梦通道"}], msgs)
    assert r == [(1, 3)], r
    print("ok  test_spans_from_quotes_half_valid")


def test_spans_from_quotes_nearby_boundaries():
    # 2026-10-17 momo 改定：单端定位 → 就近 10 条消息（不再是会话
    # 尽头的保证边界）；首尾同一句 → ±3 轮（与 read_source 同口径）。
    from ember.turn_span import spans_from_quotes
    msgs = [{"role": "user" if i % 2 == 0 else "assistant", "turn": i + 1,
             "content": f"消息{i}的内容"}
            for i in range(24)]   # 24 条消息，turn 1..24

    # 只有首句（turn 5）→ 往后 10 条消息 → turn 15
    r = spans_from_quotes(
        [{"start_quote": "消息4的内容", "end_quote": "不存在"}], msgs)
    assert r == [(5, 15)], r
    # 只有尾句（turn 20）→ 往前 10 条消息 → turn 10
    r = spans_from_quotes(
        [{"start_quote": "不存在", "end_quote": "消息19的内容"}], msgs)
    assert r == [(10, 20)], r
    # 只有首句且尾部不足 10 条（turn 22）→ 到语料末尾 turn 24
    r = spans_from_quotes(
        [{"start_quote": "消息21的内容", "end_quote": "不存在"}], msgs)
    assert r == [(22, 24)], r
    # 首尾同一句（turn 8）→ ±3 轮（5..11）
    r = spans_from_quotes(
        [{"start_quote": "消息7的内容", "end_quote": "消息7的内容"}], msgs)
    assert r == [(5, 11)], r
    # 同一句贴开头（turn 1）→ 不足有多少取多少（1..4）
    r = spans_from_quotes(
        [{"start_quote": "消息0的内容", "end_quote": "消息0的内容"}], msgs)
    assert r == [(1, 4)], r
    print("ok  test_spans_from_quotes_nearby_boundaries")


if __name__ == "__main__":
    test_fill_and_reload()
    test_norm()
    test_v1_compat()
    test_degrade()
    test_covers()
    test_file_format_v2()
    test_spans_from_quotes_half_valid()
    test_spans_from_quotes_nearby_boundaries()
    print("turn_span tests: all passed")
