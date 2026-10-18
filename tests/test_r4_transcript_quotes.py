# -*- coding: utf-8 -*-
"""r4 交接单测试（2026-10-14 去原文化后同步）：重启恢复计数 / key_quotes /
read_memory_source（拉取版）。

transcripts.jsonl 已退役（原文只存后端一份，ember 按 message_id 按需拉取）：
原「存档」类断言改为退役语义（不再落盘/账本记账/拉取语料）。
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember.store import EmberStore  # noqa: E402
from ember.models import Event  # noqa: E402
from ember.writer import validate_writer_arguments  # noqa: E402
from ember.service import EmberService, MemoryWriteStatus  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"PASS - {name}")
    else:
        failures.append(name)
        print(f"FAIL - {name}  {detail}")


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class FakeClient:
    """最小 mock：第一轮触发 key_quotes 写入，之后普通回复。"""

    def __init__(self, writer_args=None, fail_first=False):
        self.writer_args = writer_args
        self.fail_first = fail_first
        self.calls = 0

    def chat_completion(self, model, messages, tools=None, **kw):
        self.calls += 1
        if self.fail_first and self.calls == 1:
            from ember.llm import LLMError
            raise LLMError("provider down")
        has_tool = bool(tools)
        if has_tool and self.writer_args and self.calls == 1:
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "create_memory_event",
                             "arguments": json.dumps(self.writer_args,
                                                     ensure_ascii=False)},
            }]}
        if has_tool and self.writer_args and self.calls > 1:
            # tool result 后的续跑
            last = messages[-1]
            if last.get("role") == "tool":
                return {"role": "assistant", "content": "（mock）好的，继续。"}
        return {"role": "assistant", "content": f"回复#{self.calls}"}


def _mk_service(root):
    store = EmberStore(root)
    return EmberService(store, FakeClient(), model="mock")


GOOD_WRITER_ARGS = {
    "event": {
        "summary": "定稿了测试协议",
        "participants": ["ai", "用户"], "objects": [],
        "context": "测试会话",
        "core": "我们定稿了测试协议。",
        "outcome": "定稿", "state": "", "affect": "",
        "significance": "8：定稿了测试协议",
        "details": [],
        "key_quotes": [
            {"speaker": "user", "text": "就按这个方案定稿吧。"},
            {"speaker": "ai", "text": "好的，测试协议 v1 就这么定了。"},
            {"speaker": "user", "text": "这句是编造的不在原文里"},
        ],
    },
    "recall_routes": [{"cue_type": "lexical", "cue": "测试协议"}],
    "quote_spans": [{"start_quote": "就按这个方案定稿吧。",
                     "end_quote": "好的，测试协议 v1 就这么定了。"}],
}


def test_chat_archives_and_failure():
    with tempfile.TemporaryDirectory() as root:
        svc = _mk_service(root)
        payload = {"session_id": "sA", "model": "mock",
                   "messages": [{"role": "user", "content": "就按这个方案定稿吧。"}]}
        resp = svc.chat_completion(payload)
        # 2026-10-14 transcripts 退役：chat 不再落原文存档，
        # 轮次持久账 = turn_ledger（原文按 message_id 按需拉取）
        check("成功轮不再写存档",
              not os.path.exists(os.path.join(root, "transcripts.jsonl")))
        check("轮次落账本（无条件记账）",
              svc.turn_ledger.max_turn("sA") == 1)
        check("user 行不含召回前缀",
              all(not m["content"].startswith("[Ember")
                  for m in svc.sessions["sA"].messages
                  if m.get("role") == "user"))
        # chat 失败：LLM 抛错 → 不记账不存档
        svc2 = EmberService(EmberStore(root), FakeClient(fail_first=True))
        try:
            svc2.chat_completion({"session_id": "sB", "model": "mock",
                                  "messages": [{"role": "user", "content": "x"}]})
            raised = False
        except Exception:
            raised = True
        check("chat 失败抛 LLMError", raised)
        check("失败轮不写存档",
              not os.path.exists(os.path.join(root, "transcripts.jsonl")))


def test_restart_resume_counter():
    with tempfile.TemporaryDirectory() as root:
        svc = _mk_service(root)
        for i in range(1, 4):
            svc.chat_completion({"session_id": "sR", "model": "mock",
                                 "messages": [{"role": "user",
                                               "content": f"第{i}轮"}]})
        # 模拟重启：全新 EmberService 读同一目录
        svc2 = EmberService(EmberStore(root), FakeClient(), model="mock")
        sess = svc2.get_or_create_session("sR")
        check("重启后计数恢复到最大 turn（账本恢复）",
              sess.turn_counter == 3, f"counter={sess.turn_counter}")
        check("重启后 messages 为空（上下文恢复=独立项）",
              sess.messages == [])
        resp = svc2.chat_completion({"session_id": "sR", "model": "mock",
                                     "messages": [{"role": "user",
                                                   "content": "重启后第一句"}]})
        check("重启后下一轮 turn = 原最大 + 1", resp["ember"]["turn"] == 4,
              str(resp["ember"]["turn"]))
        check("重启后轮次账本接续",
              svc2.turn_ledger.max_turn("sR") == 4)


def test_key_quotes_validation():
    ok = json.loads(json.dumps(GOOD_WRITER_ARGS))
    v, err = validate_writer_arguments(ok)
    check("3 条合法通过", v is not None, err or "")
    four = json.loads(json.dumps(GOOD_WRITER_ARGS))
    four["event"]["key_quotes"].append({"speaker": "ai", "text": "第四句"})
    v2, err2 = validate_writer_arguments(four)
    check("4 条整条拒绝", v2 is None and "最多 3 条" in (err2 or ""), err2 or "")
    bad = json.loads(json.dumps(GOOD_WRITER_ARGS))
    bad["event"]["key_quotes"][0]["speaker"] = "other"
    v3, err3 = validate_writer_arguments(bad)
    check("speaker 非法拒绝", v3 is None and "speaker" in (err3 or ""))
    empty = json.loads(json.dumps(GOOD_WRITER_ARGS))
    empty["event"]["key_quotes"] = []
    v4, err4 = validate_writer_arguments(empty)
    check("空数组合法", v4 is not None and v4["event"]["key_quotes"] == [])
    # 旧事件（无 key_quotes 字段）读取兼容
    old = {"event_id": "EVT-099-zzz", "version_id": "v", "source_ref": "s",
           "raw_ref": "r", "event_time": _now_iso(), "ingest_time": _now_iso(),
           "summary": "旧", "core": "旧"}
    e = Event.from_dict(old)
    check("旧事件 from_dict 兼容（key_quotes 默认空）", e.key_quotes == [])


def test_verify_and_presentation():
    with tempfile.TemporaryDirectory() as root:
        store = EmberStore(root)
        client = FakeClient(writer_args=GOOD_WRITER_ARGS)
        svc = EmberService(store, client, model="mock")
        resp = svc.chat_completion({
            "session_id": "sV", "model": "mock",
            "messages": [{"role": "user",
                          "content": "就按这个方案定稿吧。\n怎么都行，就按这个方案定稿吧。"}]})
        # 第一轮 assistant 回复是 mock 的"回复#1"——AI 原话对不上会被丢
        mem = resp["ember"]["memory"]
        ev = store.all_events()[-1]
        kept_speakers = [q["speaker"] for q in ev.key_quotes]
        check("对得上的保留（user 句在原文）",
              "user" in kept_speakers, str(ev.key_quotes))
        check("对不上的丢弃（ai 句不在原文）",
              "ai" not in kept_speakers, str(ev.key_quotes))
        check("编造句被丢弃", all("编造" not in q["text"]
                                  for q in ev.key_quotes))
        check("dropped_quotes 进 tool result（memory 状态可见）",
              len(mem.get("dropped_quotes", [])) >= 1,
              str(mem.get("dropped_quotes")))
        # 事件落盘的 key_quotes 与丢弃后一致
        check("Event 落盘的是核对后的子集",
              len(ev.key_quotes) == 3 - len(mem.get("dropped_quotes", [])))


def test_read_memory_source():
    with tempfile.TemporaryDirectory() as root:
        store = EmberStore(root)
        client = FakeClient(writer_args=GOOD_WRITER_ARGS)
        svc = EmberService(store, client, model="mock")
        svc.chat_completion({
            "session_id": "sQ", "model": "mock",
            "messages": [{"role": "user", "content": "就按这个方案定稿吧。"}]})
        ev = store.all_events()[-1]
        # 直接调用处理函数（绕过 LLM 工具循环）
        call = {"function": {"name": "read_memory_source",
                             "arguments": json.dumps({"event_id": ev.event_id})}}
        out = svc._handle_read_source_call(svc.get_or_create_session("sQ"), [call])
        r = store.root  # 占位避免 lint
        # 从 session 历史里取 tool 结果
        tool_msg = [m for m in svc.sessions["sQ"].messages
                    if m.get("role") == "tool"][-1]
        content = json.loads(tool_msg["content"])
        check("有存档返回 ok + 原文",
              content["status"] == "ok"
              and "就按这个方案定稿吧" in content["transcript"],
              str(content)[:120])
        # 无存档（旧事件）
        old = Event(event_id="EVT-098-yyyy", version_id="v",
                    source_ref="conversation/session-ghost",
                    raw_ref="r", event_time=_now_iso(),
                    ingest_time=_now_iso(), summary="旧", core="旧")
        store.write_event(old)
        call2 = {"function": {"name": "read_memory_source",
                              "arguments": json.dumps({"event_id": "EVT-098-yyyy"})}}
        svc._handle_read_source_call(svc.get_or_create_session("sQ"), [call2])
        tool_msg2 = [m for m in svc.sessions["sQ"].messages
                     if m.get("role") == "tool"][-1]
        content2 = json.loads(tool_msg2["content"])
        check("无存档 → not_archived 不报错",
              content2["status"] == "not_archived", str(content2)[:120])
        # 超长截断（2026-10-14 改源：语料经 source_client 拉取）
        with tempfile.TemporaryDirectory() as root2:
            long_text = "很长的原文" * 2000
            store2 = EmberStore(root2)
            ev2 = Event(event_id="EVT-097-xxxx", version_id="v",
                        source_ref="conversation/session-sL",
                        raw_ref="r", event_time=_now_iso(),
                        ingest_time=_now_iso(), summary="长", core="长",
                        turn_refs=["turn-1"])
            store2.write_event(ev2)
            # 出处区间显式回填（2026-10-12 裁决移除锚点代答后，翻原文
            # 依赖真实 span；本测试意图是截断行为，区间给 (1,1) 即可）
            from ember.turn_span import TurnSpanStore
            TurnSpanStore(root2).fill("EVT-097-xxxx", [(1, 1)])
            svc3 = EmberService(store2, FakeClient(), model="mock")
            # mock 拉取：账本登记第 1 轮的 message_id，原文接口返回长文本
            svc3.turn_ledger.record("sL", 1, None, user_message_id=500)
            svc3.turn_ledger.record_reply("sL", 1, assistant_message_id=501)
            from ember import source_client as sc
            orig_fetch = sc.fetch_source_range
            sc.fetch_source_range = lambda sid, a, b: [
                {"message_id": 500, "role": "user", "content": long_text},
                {"message_id": 501, "role": "assistant", "content": long_text},
            ]
            try:
                call3 = {"function": {"name": "read_memory_source",
                                      "arguments": json.dumps(
                                          {"event_id": "EVT-097-xxxx"})}}
                svc3._handle_read_source_call(svc3.get_or_create_session("sL"),
                                              [call3])
            finally:
                sc.fetch_source_range = orig_fetch
            tool_msg3 = [m for m in svc3.sessions["sL"].messages
                         if m.get("role") == "tool"][-1]
            content3 = json.loads(tool_msg3["content"])
            check("超长截断并注明",
                  content3["status"] == "ok" and content3["truncated"] is True
                  and len(content3["transcript"])
                  <= EmberService.READ_SOURCE_MAX_CHARS)
        # unknown id
        call4 = {"function": {"name": "read_memory_source",
                              "arguments": json.dumps({"event_id": "EVT-000-nope"})}}
        svc._handle_read_source_call(svc.get_or_create_session("sQ"), [call4])
        tool_msg4 = [m for m in svc.sessions["sQ"].messages
                     if m.get("role") == "tool"][-1]
        content4 = json.loads(tool_msg4["content"])
        check("未知 id → not_found", content4["status"] == "not_found")


def test_presentation_lines():
    """呈现层直接测 _build_recall_prefix 的原话行与工具指引。"""
    with tempfile.TemporaryDirectory() as root:
        store = EmberStore(root)
        ev = Event(event_id="EVT-096-wwww", version_id="v",
                   source_ref="conversation/session-s", raw_ref="r",
                   event_time=_now_iso(), ingest_time=_now_iso(),
                   summary="测试", core="第一人称叙述。",
                   key_quotes=[{"speaker": "user", "text": "你说的那句"},
                               {"speaker": "ai", "text": "我说的这句"}])
        store.write_event(ev)
        svc = EmberService(store, FakeClient(), model="mock")
        from ember.models import PresentationContext, RecallTrace
        ctx = PresentationContext(recalled_events=["EVT-096-wwww"],
                                  recall_trace=RecallTrace(input_text="x"))
        prefix = svc._build_recall_prefix(ctx)
        check("前缀含原话行（她/我 标注）",
              "原话：「你说的那句」（她）「我说的这句」（我）" in prefix,
              prefix[:200])
        check("前缀指向 read_memory_source",
              'read_memory_source("EVT-096-wwww")' in prefix)
        # summary_fallback：不带原话行
        prefix2 = svc._build_recall_prefix(ctx, summary_fallback=True)
        check("summary_fallback 不带原话", "原话：" not in prefix2)


def test_inline_source_high_match():
    """高匹配内联【当时对话】（2026-10-18 momo 定稿：三条件合取，要罕见）。

    sig 8-9 + 本轮 ≥2 独立线索 + 语义锚点在（过值即锚点）→ 内联
    span 区间（±3 轮扩展）的当时对话；任一条件缺失 → 原话行照旧。
    """
    from ember.models import PresentationContext, RecallTrace, RouteHit

    def _hit(cue, matcher="lexical", eid="EVT-097"):
        return RouteHit(route_id=f"rt-{cue}-{matcher}", event_id=eid,
                        cue=cue, matcher=matcher, cue_type="lexical")

    def _mk(svc, eid="EVT-097"):
        """标准高匹配事件：sig 9、span (5,5)、内存铺 turn 2-8 原文。"""
        svc.get_or_create_session("s").messages = [
            {"role": "user" if t % 2 else "assistant", "turn": t,
             "content": f"轮{t}的原话"}
            for t in range(2, 9)]
        svc.store.write_event(Event(
            event_id=eid, version_id="v",
            source_ref="conversation/session-s",
            raw_ref="conversation/session-s/turn-5",
            event_time=_now_iso(), ingest_time=_now_iso(),
            summary="重要发现的摘要", core="重要发现的完整叙述。",
            significance="9：影响后续功能设计",
            key_quotes=[{"speaker": "user", "text": "轮5的原话"}]))
        svc.turn_spans.fill(eid, [(5, 5)], filled_by="test")

    def _prefix(svc, anchors):
        ctx = PresentationContext(
            recalled_events=["EVT-097"],
            recall_trace=RecallTrace(input_text="x", anchors=anchors))
        return svc._build_recall_prefix(ctx)

    with tempfile.TemporaryDirectory() as root:
        svc = EmberService(EmberStore(root), FakeClient(), model="mock")
        _mk(svc)
        # 正例：双线索（其一语义）+ sig 9 → 内联当时对话（±3 轮扩展）
        p = _prefix(svc, [_hit("追剧"), _hit("更新", "semantic")])
        check("三条件全满足 → 内联【当时对话】", "【当时对话】" in p, p[:300])
        check("内联含 span 原文（±3 轮扩展）",
              "[turn 4] assistant: 轮4的原话" in p, p[:300])
        check("内联后不再出原话行", "原话：" not in p, p[:300])
        check("内联后无深挖提示", "需要原话时" not in p, p[:300])

        # 反例 1：sig 6（低于 8）
        svc2 = EmberService(EmberStore(root), FakeClient(), model="mock")
        _mk(svc2, "EVT-098")
        ev = svc2.store.get_event("EVT-098")
        ev.significance = "6：背景级"
        svc2.store.write_event(ev)
        svc2.turn_spans.fill("EVT-098", [(5, 5)], filled_by="test")
        ctx = PresentationContext(
            recalled_events=["EVT-098"],
            recall_trace=RecallTrace(input_text="x", anchors=[
                _hit("追剧", eid="EVT-098"),
                _hit("更新", "semantic", eid="EVT-098")]))
        p2 = svc2._build_recall_prefix(ctx)
        check("sig 6 → 不内联，走原话行",
              "【当时对话】" not in p2 and "原话：" in p2, p2[:300])

        # 反例 2：单线索（语义 lone hit）
        p3 = _prefix(svc, [_hit("追剧", "semantic")])
        check("单线索 → 不内联",
              "【当时对话】" not in p3 and "原话：" in p3, p3[:300])

        # 反例 3：双线索但全是词面（无语义锚点）
        p4 = _prefix(svc, [_hit("追剧"), _hit("更新")])
        check("无语义锚点 → 不内联",
              "【当时对话】" not in p4 and "原话：" in p4, p4[:300])


def main():
    test_chat_archives_and_failure()
    test_restart_resume_counter()
    test_key_quotes_validation()
    test_verify_and_presentation()
    test_read_memory_source()
    test_presentation_lines()
    test_inline_source_high_match()
    total = 35
    if failures:
        print(f"\n{total - len(failures)}/{total} checks passed，失败：{failures}")
        sys.exit(1)
    print(f"\n{total}/{total} checks passed")


if __name__ == "__main__":
    main()
