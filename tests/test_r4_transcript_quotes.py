# -*- coding: utf-8 -*-
"""r4 交接单测试：原文存档 / 重启恢复计数 / key_quotes / read_memory_source。

覆盖（对应交接单"验证"清单）：
1. TranscriptStore：append/get 按轮升序/max_turn/has_session
2. chat 成功后 transcripts 多两行、user 行不含召回前缀（mock client 全链）
3. chat 失败（LLMError）不写存档
4. 重启恢复计数：新建 EmberService 读同一目录，同 session 下一轮
   turn = 原最大 turn + 1；messages 为空（上下文恢复=独立项）
5. key_quotes：校验（>3 条整条拒绝/字段校验/空数组合法）；
   逐字核对（对得上保留、对不上丢弃并进 dropped_quotes→tool result）
6. 呈现：召回前缀出现原话行（她/我 标注）与 read_memory_source 指引；
   summary_fallback 不出现原话行
7. read_memory_source：有存档返回原文（turn_spans 区间）；无存档
   not_archived；超长截断；unknown id → not_found
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember.transcript import TranscriptStore  # noqa: E402
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
        "outcome": "定稿", "state": "", "affect": "", "significance": "",
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


def test_transcript_store():
    with tempfile.TemporaryDirectory() as root:
        ts = TranscriptStore(root)
        ts.append_turn("s1", 1, "用户一", "回复一")
        ts.append_turn("s1", 3, "用户三", "回复三")
        ts.append_turn("s2", 1, "另会话", "另回复")
        rows = ts.get("s1", [3, 1])
        check("get 按 turn 升序 + 同轮 user 在前",
              [r["turn"] for r in rows] == [1, 1, 3, 3]
              and rows[0]["role"] == "user" and rows[1]["role"] == "assistant",
              str([(r["turn"], r["role"]) for r in rows]))
        s2_rows = ts.get("s2", [1])
        check("跨 session 隔离",
              len(s2_rows) == 2 and s2_rows[0]["content"] == "另会话",
              str([(r["turn"], r["role"]) for r in s2_rows]))
        check("max_turn / has_session",
              ts.max_turn("s1") == 3 and ts.has_session("s1")
              and ts.max_turn("nope") == 0 and not ts.has_session("nope"))
        check("无文件 get 为空", ts.get("ghost", [1, 2]) == [])


def test_chat_archives_and_failure():
    with tempfile.TemporaryDirectory() as root:
        svc = _mk_service(root)
        payload = {"session_id": "sA", "model": "mock",
                   "messages": [{"role": "user", "content": "就按这个方案定稿吧。"}]}
        resp = svc.chat_completion(payload)
        rows = TranscriptStore(root).get("sA", [1])
        check("成功轮存档两行", len(rows) == 2
              and rows[0]["content"] == "就按这个方案定稿吧。"
              and rows[1]["role"] == "assistant", str(rows))
        check("user 行不含召回前缀",
              not rows[0]["content"].startswith("[Ember"))
        # chat 失败：LLM 抛错 → 不写存档
        svc2 = EmberService(EmberStore(root), FakeClient(fail_first=True))
        try:
            svc2.chat_completion({"session_id": "sB", "model": "mock",
                                  "messages": [{"role": "user", "content": "x"}]})
            raised = False
        except Exception:
            raised = True
        check("chat 失败抛 LLMError", raised)
        check("失败轮不写存档", TranscriptStore(root).get("sB", [1]) == [])


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
        check("重启后计数恢复到最大 turn",
              sess.turn_counter == 3, f"counter={sess.turn_counter}")
        check("重启后 messages 为空（上下文恢复=独立项）",
              sess.messages == [])
        resp = svc2.chat_completion({"session_id": "sR", "model": "mock",
                                     "messages": [{"role": "user",
                                                   "content": "重启后第一句"}]})
        check("重启后下一轮 turn = 原最大 + 1", resp["ember"]["turn"] == 4,
              str(resp["ember"]["turn"]))
        check("重启后轮次存档接续",
              TranscriptStore(root).max_turn("sR") == 4)


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
        # 超长截断
        with tempfile.TemporaryDirectory() as root2:
            ts = TranscriptStore(root2)
            long_text = "很长的原文" * 2000
            ts.append_turn("sL", 1, long_text, long_text)
            store2 = EmberStore(root2)
            ev2 = Event(event_id="EVT-097-xxxx", version_id="v",
                        source_ref="conversation/session-sL",
                        raw_ref="r", event_time=_now_iso(),
                        ingest_time=_now_iso(), summary="长", core="长",
                        turn_refs=["turn-1"])
            store2.write_event(ev2)
            svc3 = EmberService(store2, FakeClient(), model="mock")
            call3 = {"function": {"name": "read_memory_source",
                                  "arguments": json.dumps(
                                      {"event_id": "EVT-097-xxxx"})}}
            svc3._handle_read_source_call(svc3.get_or_create_session("sL"),
                                          [call3])
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


def main():
    test_transcript_store()
    test_chat_archives_and_failure()
    test_restart_resume_counter()
    test_key_quotes_validation()
    test_verify_and_presentation()
    test_read_memory_source()
    test_presentation_lines()
    total = 29
    if failures:
        print(f"\n{total - len(failures)}/{total} checks passed，失败：{failures}")
        sys.exit(1)
    print(f"\n{total}/{total} checks passed")


if __name__ == "__main__":
    main()
