# -*- coding: utf-8 -*-
"""pair 留痕（pair_seen）测试（架构文档 §4.2）。

覆盖：mark/is_seen 往返、无向键规范化、重载重建、文件格式。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.pair_seen import PairSeenStore, pair_key


def test_mark_and_seen():
    with tempfile.TemporaryDirectory() as root:
        ps = PairSeenStore(root)
        ps.mark("EVT-008", "EVT-001")
        assert ps.is_seen("EVT-001", "EVT-008")   # 无向：参数顺序无关
        assert not ps.is_seen("EVT-001", "EVT-099")
        ps2 = PairSeenStore(root)
        assert ps2.is_seen("EVT-008", "EVT-001")  # 重载可重建
        assert ps2.count() == 1
        print("ok  test_mark_and_seen")


def test_pair_key():
    assert pair_key("EVT-008", "EVT-001") == "EVT-001|EVT-008"
    assert pair_key("EVT-001", "EVT-008") == "EVT-001|EVT-008"
    print("ok  test_pair_key")


def test_file_format():
    with tempfile.TemporaryDirectory() as root:
        ps = PairSeenStore(root)
        ps.mark("EVT-001", "EVT-002", decided_by="sleep-v2-test")
        p = os.path.join(root, "pair_seen.jsonl")
        with open(p, encoding="utf-8") as f:
            line = json.loads(f.readline())
        assert line["pair_key"] == "EVT-001|EVT-002"
        assert line["verdict"] == "none"
        assert {"pair_key", "verdict", "decided_by", "recorded_at"} <= set(line)
        print("ok  test_file_format")


if __name__ == "__main__":
    test_mark_and_seen()
    test_pair_key()
    test_file_format()
    print("pair_seen tests: all passed")
