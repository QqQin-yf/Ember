# -*- coding: utf-8 -*-
"""heart 联动通电测试（momo 2026-10-06）。

验证点：
1. TurnLedger：heart_meta.turn_id 记账 / 缺失不写账 / 反向查询 / 缺口观察
2. TurnSpanStore：未回填降级（turn_refs 锚点）→ 回填精确区间 → 覆盖
3. fragment_source：只带出处不带感受（铁律：感受指纹 heart 现算）
4. 铁律检查：账本/span 均为旁路文件，Event 本体零改动
"""
import sys, os, json, tempfile, shutil

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember.turn_ledger import TurnLedger  # noqa: E402
from ember.turn_span import TurnSpanStore  # noqa: E402

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


root = tempfile.mkdtemp(prefix="ember_turn_test_")
try:
    # ---------- 1. TurnLedger ----------
    ledger = TurnLedger(root)
    e1 = ledger.record("s1", 1, "t-000001")
    e2 = ledger.record("s1", 2, "t-000002")
    check("记账成功（返回条目）", e1 is not None and e2 is not None)
    check("正向查询", ledger.heart_id_of("s1", 2) == "t-000002")
    check("反向查询", ledger.ember_turns_of("t-000001") == [("s1", 1)])
    check("未登记返回 None", ledger.heart_id_of("s1", 99) is None)

    # 缺失 heart_meta：不写账、不报错
    check("无 turn_id 不写账", ledger.record("s1", 3, None) is None)
    check("空串不写账", ledger.record("s1", 3, "  ") is None)

    # 落盘重载：映射持久
    ledger2 = TurnLedger(root)
    check("重载后映射仍在", ledger2.heart_id_of("s1", 1) == "t-000001")

    # 缺口观察：跳过 t-000003 给 t-000004
    ledger.record("s1", 4, "t-000004")
    gaps = ledger.gaps()
    check(f"发号缺口被观察（{gaps}）", gaps == ["t-000003"])

    # 同键重登记（重发场景）：append-only 留痕，映射取最新
    ledger.record("s1", 4, "t-000005")
    check("同 ember turn 重登记取最新", ledger.heart_id_of("s1", 4) == "t-000005")
    check("重登记反向索引多值", ledger.ember_turns_of("t-000004") == [("s1", 4)])

    # ---------- 2. TurnSpanStore ----------
    spans = TurnSpanStore(root)
    # 降级路径：用锚点
    class _FakeEvent:
        event_id = "EVT-001-aaaaaa"
        turn_refs = ["turn-3"]

    s = spans.span_of_event(_FakeEvent())
    check("未回填降级到锚点单点区间", s == [(3, 3)])

    class _FakeEvent2:
        event_id = "EVT-002-bbbbbb"
        turn_refs = ["turn-2", "turn-7"]

    check("未回填降级到锚点 min/max", spans.span_of_event(_FakeEvent2()) == [(2, 7)])

    class _FakeEvent3:
        event_id = "EVT-003-cccccc"
        turn_refs = []

    check("无锚点且未回填 → 空列表", spans.span_of_event(_FakeEvent3()) == [])

    # 回填（A2 起为区间列表 API：fill(event_id, [(s,e), ...])）
    spans.fill("EVT-001-aaaaaa", [(3, 6)], filled_by="test")
    check("回填后精确区间", spans.span_of_event(_FakeEvent()) == [(3, 6)])

    # 非法区间拒绝
    try:
        spans.fill("EVT-001-aaaaaa", [(6, 3)])
        check("非法区间被拒绝", False)
    except ValueError:
        check("非法区间被拒绝", True)

    # 覆盖重填（append-only，取最新）
    spans.fill("EVT-001-aaaaaa", [(3, 8)], filled_by="test-v2")
    check("重填取最新", spans.span_of_event(_FakeEvent()) == [(3, 8)])

    # 重载持久
    spans2 = TurnSpanStore(root)
    check("重载后 span 仍在", spans2.span_of("EVT-001-aaaaaa") == [(3, 8)])

    # ---------- 3. dream 选材（v3：fragment_source/Fragment 协议已废除） ----------
    from ember.store import EmberStore  # noqa: E402
    from ember.service import EmberService  # noqa: E402

    data_root = os.path.join(BASE, "data_real")
    store = EmberStore(data_root)

    class _FakeClient:
        def chat_completion(self, **kw):
            raise RuntimeError("test does not call LLM")

    svc = EmberService(store, _FakeClient(), model="test")
    sel = svc.dream_material_preview()
    check("dream 选材产出结构完整",
          all(k in sel for k in ("events", "pushed_ids", "skipped",
                                 "associated_ids", "random_id")))
    for e in sel["events"]:
        check("材料事件仅 text+turn_span 两字段（v3 契约）",
              set(e) == {"text", "turn_span"}
              and isinstance(e["turn_span"], list)
              and len(e["turn_span"]) == 2
              and e["turn_span"][0].startswith("t-")
              and e["turn_span"][1].startswith("t-"), str(e))
    check("材料不带感受字段（铁律）",
          not any(k in json.dumps(sel) for k in
                  ("salience", "affect_snapshot", "origin_snapshot")))

    # ---------- 4. 铁律：Event 本体零改动 ----------
    eid = store.all_events()[0].event_id
    ev_dict = store.get_event(eid).to_dict()
    check("Event 本体无 heart 派生字段",
          not any(k in ev_dict for k in ("heart_turn_id", "turn_span",
                                         "turn_start", "dreamed")))
finally:
    shutil.rmtree(root, ignore_errors=True)

fails = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(fails)}/{len(checks)} checks passed")
if fails:
    print("FAILED:", fails)
    sys.exit(1)
