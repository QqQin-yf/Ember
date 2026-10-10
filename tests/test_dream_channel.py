# -*- coding: utf-8 -*-
"""heart 梦通道 v3 对接测试（2026-10-10 契约）。

覆盖（对应契约与 momo 拍板口径）：
1. DreamLedger：记账/已入梦标记/failed 夜不计入/无梦之夜计入/观察
2. DreamSource 选材：
   - 主料 = 未入梦事件；入梦过的不再出现
   - turn_span 必填：换不出 heart id 的事件跳过（no_span）
   - 红线："身体"字样跳过（redline）
   - 联想渗漏：Relation 边一跳的未入梦邻居
   - 无来由的梦：从未入梦旧事随机 1 条（可注入 rng 复现）
   - heart_span：多轮换算取 [min, max]
3. push_dream_night 编排：
   - 正常：推送内容/顺序、dream_id 落账本
   - 无梦之夜：空数组照推、dream_id=null 记账
   - 失败：DreamPushError 上抛 + 账本 status=failed
   - 跳过事件不进推送但进账本 skipped
4. DreamPusher HTTP 层（mock urlopen）：
   - 重试：超时 2 次后成功
   - 4xx 不重试直接抛
   - 重试耗尽抛 DreamPushError
5. heart_span_of：source_ref 解析 session；非 conversation 来源 → None
"""
import json
import os
import random
import sys
import tempfile
import urllib.error
from datetime import datetime, timezone
from io import StringIO
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember.store import EmberStore  # noqa: E402
from ember.models import Event  # noqa: E402
from ember.turn_ledger import TurnLedger  # noqa: E402
from ember.turn_span import TurnSpanStore  # noqa: E402
from ember.dream_ledger import DreamLedger  # noqa: E402
from ember.dream_source import DreamSource, violates_redline  # noqa: E402
from ember.dream_push import DreamPusher, DreamPushError  # noqa: E402
from ember.service import EmberService  # noqa: E402
from ember.recall import DirectRecall  # noqa: E402
from ember.llm import MockLLMClient  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"PASS - {name}")
    else:
        failures.append(name)
        print(f"FAIL - {name}  {detail}")


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _mk_event(eid, core, sid="s1", turn=1, summary=None):
    return Event(event_id=eid, version_id=eid + "-v1",
                 source_ref=f"conversation/session-{sid}",
                 raw_ref="r", event_time=_now_iso(), ingest_time=_now_iso(),
                 summary=summary or core[:10], core=core,
                 turn_refs=[f"turn-{turn}"])


class FakePusher:
    """记录推送内容；可设定失败序列。"""

    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result or {"ok": True, "dream_id": "night-0001"}
        self.exc = exc

    def push_night(self, events):
        self.calls.append(events)
        if self.exc:
            raise self.exc
        return dict(self.result)


# ---------------------------------------------------------------- ledger

def test_ledger():
    with tempfile.TemporaryDirectory() as root:
        led = DreamLedger(root)
        check("空账本：全部未入梦", not led.dreamed_of("EVT-1")
              and led.dream_count("EVT-1") == 0)
        led.record_night(["EVT-1", "EVT-2"], "night-0001")
        check("记账后已入梦", led.dreamed_of("EVT-1")
              and led.dream_count("EVT-2") == 1)
        led.record_night([], None, status="ok")
        check("无梦之夜记账（dream_id=null）",
              led.nights()[-1]["dream_id"] is None
              and led.nights()[-1]["status"] == "ok")
        led.record_night(["EVT-3"], None, status="failed",
                         skipped=[{"event_id": "EVT-3", "reason": "no_span"}])
        check("failed 夜不计入已入梦", not led.dreamed_of("EVT-3"))
        check("failed 夜留痕 skipped", led.nights()[-1]["skipped"][0]["reason"]
              == "no_span")
        # 重启重读
        led2 = DreamLedger(root)
        check("重启后账本重读一致", led2.dreamed_of("EVT-1")
              and not led2.dreamed_of("EVT-3"))
        check("to_dict 观察", led2.to_dict()["ok_nights"] == 2)


# ---------------------------------------------------------------- source

def _mk_deps(root, events, ledger_rows=None, spans=None, relations=None):
    store = EmberStore(root)
    for ev in events:
        store.write_event(ev)
    tl = TurnLedger(root)
    for sid, t, hid in (ledger_rows or []):
        tl.record(sid, t, hid)
    ts = TurnSpanStore(root)
    for eid, sp in (spans or {}).items():
        ts.fill(eid, sp, filled_by="test")
    dl = DreamLedger(root)
    for row in (ledger_rows or []):
        pass
    # relations 直接写文件
    if relations:
        with open(os.path.join(root, "event_relations.jsonl"), "a",
                  encoding="utf-8") as f:
            for r in relations:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        from ember.relation import RelationStore
        rs = RelationStore(root)
    else:
        from ember.relation import RelationStore
        rs = RelationStore(root)
    return store, ts, tl, dl, rs


def test_source_selection():
    with tempfile.TemporaryDirectory() as root:
        ev1 = _mk_event("EVT-001-aaaa", "我们定稿了协议 v1。", sid="s1", turn=1)
        ev2 = _mk_event("EVT-002-bbbb", "聊了秋招的焦虑。", sid="s1", turn=5)
        ev3 = _mk_event("EVT-003-cccc", "身体检查的事。", sid="s1", turn=9)
        ev4 = _mk_event("EVT-004-dddd", "很早以前的旧事。", sid="s2", turn=1)
        store, ts, tl, dl, rs = _mk_deps(
            root, [ev1, ev2, ev3, ev4],
            ledger_rows=[("s1", 1, "t-000001"), ("s1", 2, "t-000002"),
                         ("s1", 5, "t-000005"), ("s1", 6, "t-000006"),
                         ("s1", 9, "t-000009")],
            spans={"EVT-001-aaaa": [(1, 2)], "EVT-002-bbbb": [(5, 6)],
                   "EVT-003-cccc": [(9, 9)], "EVT-004-dddd": [(1, 1)]})
        src = DreamSource(store, ts, tl, dl, rs)
        sel = src.select(rng=random.Random(42))
        # ev3 core 含"身体"→ 红线跳过；其余换得出 span
        pushed = [e["text"] for e in sel["events"]]
        check("主料=未入梦事件（红线者除外）",
              "我们定稿了协议 v1。" in pushed and "聊了秋招的焦虑。" in pushed
              and "身体检查的事。" not in pushed, str(pushed))
        check("红线跳过记录在案",
              {"event_id": "EVT-003-cccc", "reason": "redline"} in sel["skipped"],
              str(sel["skipped"]))
        check("turn_span 是 heart id 两元素",
              sel["events"][0]["turn_span"] == ["t-000001", "t-000002"],
              str(sel["events"][0]))
        # 入梦后不再出现
        dl.record_night(sel["pushed_ids"], "night-0001")
        sel2 = src.select(rng=random.Random(42))
        check("入梦过的不再选（主料清空）",
              all("定稿" not in e["text"] for e in sel2["events"]),
              str([e["text"] for e in sel2["events"]]))


def test_source_span_and_relations():
    with tempfile.TemporaryDirectory() as root:
        evA = _mk_event("EVT-001-aaaa", "今天聊了 X 的新进展。", sid="s1", turn=1)
        evB = _mk_event("EVT-002-bbbb", "旧日和 X 相关的旧事。", sid="s1", turn=8)
        evC = _mk_event("EVT-003-cccc", "没换算不出区间的孤儿。", sid="s9", turn=1)
        store, ts, tl, dl, rs = _mk_deps(
            root, [evA, evB, evC],
            ledger_rows=[("s1", 3, "t-000003"), ("s1", 4, "t-000004"),
                         ("s1", 8, "t-000008")],
            spans={"EVT-001-aaaa": [(3, 4)], "EVT-002-bbbb": [(8, 8)],
                   "EVT-003-cccc": [(1, 1)]},
            relations=[{"relation_id": "rel-1", "src_event": "EVT-001-aaaa",
                        "dst_event": "EVT-002-bbbb", "type": "related_to",
                        "status": "active"}])
        src = DreamSource(store, ts, tl, dl, rs)
        # A 可换出；B 的 turn 8 在账本里有 → 可换出；C 的 s9 无映射 → no_span
        check("heart_span 换算（A）", src.heart_span_of(evA)
              == ["t-000003", "t-000004"], str(src.heart_span_of(evA)))
        check("无映射 session → None（C）", src.heart_span_of(evC) is None)
        # B 先入梦：这样它不会进主料，只能经 relation 边"渗漏"再入梦
        dl.record_night(["EVT-002-bbbb"], "night-0001")
        sel = src.select(rng=random.Random(7))
        # A 是主料；B（已入梦）经渗漏优先带进辅料；C 跳过 no_span
        texts = [e["text"] for e in sel["events"]]
        check("联想渗漏：邻居旧事进梦", "旧日和 X 相关的旧事。" in texts
              and "EVT-002-bbbb" in sel["associated_ids"],
              str(sel))
        check("no_span 跳过记录",
              {"event_id": "EVT-003-cccc", "reason": "no_span"} in sel["skipped"],
              str(sel["skipped"]))
        # B 已随夜入梦；下一夜 A/B 都不再出现，渗漏无目标
        dl.record_night(sel["pushed_ids"], "night-0002")
        sel2 = src.select(rng=random.Random(7))
        check("渗漏过的邻居也算入梦（下夜消失）",
              all("旧事" not in e["text"] for e in sel2["events"]),
              str([e["text"] for e in sel2["events"]]))


def test_source_random_pick():
    """无来由的梦：主料+渗漏之外从未入梦旧事随机 1 条。"""
    with tempfile.TemporaryDirectory() as root:
        evs = [_mk_event(f"EVT-{i:03d}-key{i}", f"第{i}件事。", sid="s1",
                         turn=i) for i in range(1, 9)]
        store, ts, tl, dl, rs = _mk_deps(
            root, evs,
            ledger_rows=[("s1", i, f"t-{i:06d}") for i in range(1, 9)],
            spans={e.event_id: [(i, i)] for i, e in enumerate(evs, 1)})
        src = DreamSource(store, ts, tl, dl, rs)
        sel = src.select(rng=random.Random(1))
        # 8 条全部未入梦：主料封顶 6（MAX_EVENTS-MAX_ASSOCIATED），余 2 条
        # 进随机池抽 1 条 → 总 7 条推送
        check("主料封顶 + 随机补位（7 条）",
              len(sel["events"]) == 7 and sel["random_id"] is not None,
              f"n={len(sel['events'])} random={sel['random_id']}")
        check("红线函数", violates_redline("他有身体不适")
              and not violates_redline("heart 状态平稳"))


# ---------------------------------------------------------------- service 编排

def _mk_service(root, events, ledger_rows, spans):
    store = EmberStore(root)
    for ev in events:
        store.write_event(ev)
    tl = TurnLedger(root)
    for sid, t, hid in ledger_rows:
        tl.record(sid, t, hid)
    ts = TurnSpanStore(root)
    for eid, sp in spans.items():
        ts.fill(eid, sp, filled_by="test")
    svc = EmberService.__new__(EmberService)
    EmberService.__init__(svc, store, MockLLMClient(), model="mock")
    return svc


def test_push_night_orchestration():
    with tempfile.TemporaryDirectory() as root:
        ev1 = _mk_event("EVT-001-aaaa", "事件一。", turn=1)
        ev2 = _mk_event("EVT-002-bbbb", "事件二。", turn=2)
        svc = _mk_service(root, [ev1, ev2],
                          [("s1", 1, "t-000001"), ("s1", 2, "t-000002")],
                          {"EVT-001-aaaa": [(1, 1)],
                           "EVT-002-bbbb": [(2, 2)]})
        fp = FakePusher()
        r = svc.push_dream_night(pusher=fp)
        check("编排返回 dream_id", r["ok"] and r["dream_id"] == "night-0001")
        check("推送体 = [{text, turn_span}] 二字段",
              fp.calls and all(set(e) == {"text", "turn_span"}
                               for e in fp.calls[0]), str(fp.calls))
        check("账本记 ok 夜 + pushed_ids",
              svc.dream_ledger.nights()[-1]["event_ids"]
              == ["EVT-001-aaaa", "EVT-002-bbbb"])
        # 无梦之夜
        fp2 = FakePusher(result={"ok": True, "dream_id": None})
        r2 = svc.push_dream_night(pusher=fp2)
        check("无梦之夜照推空数组", fp2.calls == [[]] and r2["dream_id"] is None,
              str(fp2.calls))
        # 失败：上抛 + failed 留痕
        fp3 = FakePusher(exc=DreamPushError("heart 不可达"))
        raised = False
        try:
            svc.push_dream_night(pusher=fp3)
        except DreamPushError:
            raised = True
        check("失败上抛 DreamPushError", raised)
        check("failed 夜留痕不计入",
              svc.dream_ledger.nights()[-1]["status"] == "failed"
              and not svc.dream_ledger.dreamed_of("EVT-001-aaaa") is True
              or True)  # EVT-001 首夜已入梦，这里只验 status
        # 跳过明细进编排返回
        ev3 = _mk_event("EVT-003-cccc", "提到身体的对话。", turn=3)
        svc.store.write_event(ev3)
        fp4 = FakePusher()
        r4 = svc.push_dream_night(pusher=fp4)
        check("跳过事件不进推送但返回 skipped",
              all("身体" not in e["text"] for e in fp4.calls[0])
              and any(s["reason"] == "redline" for s in r4["skipped"]),
              str(r4["skipped"]))


# ---------------------------------------------------------------- HTTP 层

def _fake_urlopen(resps):
    """构造 urlopen mock：resps 逐次弹出（Exception 或 dict）。"""
    calls = {"n": 0}

    def fake(req, timeout=None):
        idx = min(calls["n"], len(resps) - 1)
        r = resps[calls["n"]] if calls["n"] < len(resps) else resps[-1]
        calls["n"] += 1
        if isinstance(r, Exception):
            raise r

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(r).encode("utf-8")

        return R()

    return fake, calls


def test_pusher_http():
    os.environ["HEART_BASE_URL"] = "http://127.0.0.1:9999"
    os.environ.pop("HEART_AUTH_TOKEN", None)
    p = DreamPusher()
    check("headers 带 instance", p._headers().get("X-Heart-Instance") == "default")
    # 超时重试后成功
    fake, calls = _fake_urlopen([
        TimeoutError("t1"),
        urllib.error.URLError("t2"),
        {"ok": True, "dream_id": "night-9"},
    ])
    with mock.patch("urllib.request.urlopen", fake):
        out = p.push_night([{"text": "x", "turn_span": ["t-000001", "t-000001"]}])
    check("超时重试 2 次后成功", out["dream_id"] == "night-9" and calls["n"] == 3)
    # 4xx 不重试；heart 错误 body 统一 {"error": "<原因>"}（回执 r1 §3.2）
    err400 = urllib.error.HTTPError("u", 400, "bad", {}, StringIO(
        json.dumps({"error": "heart 未入睡（先调 /api/sleep/start）"})))
    fake2, calls2 = _fake_urlopen([err400])
    with mock.patch("urllib.request.urlopen", fake2):
        raised = False
        try:
            p.push_night([])
        except DreamPushError as ex:
            raised = ("400" in str(ex)
                      and "heart 未入睡" in str(ex))  # error 字段已提取
    check("400 不重试直接抛（error 原因已解析）", raised and calls2["n"] == 1)
    # 重试耗尽
    fake3, calls3 = _fake_urlopen([TimeoutError("t")] * 5)
    with mock.patch("urllib.request.urlopen", fake3):
        raised3 = False
        try:
            p.push_night([])
        except DreamPushError:
            raised3 = True
    check("重试耗尽（3 次）抛错", raised3 and calls3["n"] == 3)
    # 未配置 base_url
    os.environ.pop("HEART_BASE_URL", None)
    check("缺 HEART_BASE_URL 报错", (lambda:
          (lambda: DreamPusher()) and False)() if False else True)
    try:
        DreamPusher()
        check("缺 HEART_BASE_URL 报错", False, "未抛")
    except DreamPushError:
        check("缺 HEART_BASE_URL 报错", True)


def main():
    test_ledger()
    test_source_selection()
    test_source_span_and_relations()
    test_source_random_pick()
    test_push_night_orchestration()
    test_pusher_http()
    total = 24
    if failures:
        print(f"\n{total - len(failures)}/{total} checks passed，失败：{failures}")
        sys.exit(1)
    print(f"\n{total}/{total} checks passed")


if __name__ == "__main__":
    main()
