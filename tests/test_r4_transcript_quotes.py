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

from ember.llm import MockLLMClient
from ember.store import EmberStore  # noqa: E402
from ember.models import Event  # noqa: E402
from ember.writer import validate_writer_arguments  # noqa: E402
from ember.service import EmberService  # noqa: E402

failures = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if cond:
        print(f"PASS - {name}")
    else:
        failures.append(name)
        print(f"FAIL - {name}  {detail}")


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")





def _mk_service(root):
    service = EmberService(EmberStore(root), MockLLMClient(), model="sidecar")
    service._fetched_corpus = lambda session: []
    return service



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


def test_hooks_do_not_archive_or_call_model():
    with tempfile.TemporaryDirectory() as root:
        svc = _mk_service(root)
        svc.hook_turn_start("sA", "就按这个方案定稿吧。", user_message_id=1)
        svc.hook_turn_end("sA", "确认。", assistant_message_id=2)
        check("成功轮不再写存档", not os.path.exists(os.path.join(root, "transcripts.jsonl")))
        check("轮次落账本", svc.turn_ledger.max_turn("sA") == 1)
        check("user 行不含召回前缀", svc.sessions["sA"].messages[0]["content"] == "就按这个方案定稿吧。")
        check("hooks 不调用聊天模型", svc.client.calls == [])
        check("收轮后工具不能冒写", svc.sidecar_create_memory_event(GOOD_WRITER_ARGS)["status"] == "failed")



def test_restart_resume_counter():
    with tempfile.TemporaryDirectory() as root:
        svc = _mk_service(root)
        for i in range(1, 4):
            svc.hook_turn_start("sR", f"第{i}轮", user_message_id=2*i-1)
            svc.hook_turn_end("sR", "收到。", assistant_message_id=2*i)
        restored = _mk_service(root)
        sess = restored.get_or_create_session("sR")
        check("重启后计数恢复到最大 turn", sess.turn_counter == 3)
        check("重启后 messages 为空", sess.messages == [])
        started = restored.hook_turn_start("sR", "重启后第一句", user_message_id=7)
        check("重启后下一轮 turn = 原最大 + 1", started["turn"] == 4)
        check("重启后轮次账本接续", restored.turn_ledger.max_turn("sR") == 4)



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
        svc = _mk_service(root)
        svc.hook_turn_start("sV", "就按这个方案定稿吧。")
        result = svc.sidecar_call_tool("write_event", GOOD_WRITER_ARGS)
        check("显式 MCP 写入成功", result["status"] == "created")
        ev = svc.store.get_event(result["event_id"])
        check("真 user 原话保留", ev.key_quotes == [{"speaker": "user", "text": "就按这个方案定稿吧。"}])
        check("不在原文的 AI 和编造句丢弃", len(result["dropped_quotes"]) == 2)
        reloaded = EmberStore(root).get_event(ev.event_id)
        check("落盘只保留核验后的原话", reloaded.key_quotes == ev.key_quotes)



def test_read_memory_source():
    from unittest.mock import patch
    from ember import source_client
    with tempfile.TemporaryDirectory() as root:
        svc = _mk_service(root)
        svc.hook_turn_start("sQ", "就按这个方案定稿吧。")
        result = svc.sidecar_call_tool("write_event", GOOD_WRITER_ARGS)
        ev = svc.store.get_event(result["event_id"])
        content = svc.sidecar_call_tool("read_memory_source", {"event_id": ev.event_id})
        check("旧本地语料无精确出处 → not_archived", content["status"] == "not_archived")
        svc.store.write_event(Event(event_id="EVT-098-yyyy", version_id="v",
            source_ref="conversation/session-ghost", raw_ref="r", event_time=_now_iso(),
            ingest_time=_now_iso(), summary="旧", core="旧"))
        check("无存档 → not_archived", svc.sidecar_call_tool("read_memory_source", {"event_id": "EVT-098-yyyy"})["status"] == "not_archived")
        long_text = "很长的原文" * 2000
        svc.store.write_event(Event(event_id="EVT-097-xxxx", version_id="v",
            source_ref="conversation/session-sL", raw_ref="r", event_time=_now_iso(),
            ingest_time=_now_iso(), summary="长", core="长", turn_refs=["turn-1"]))
        svc.turn_spans.fill("EVT-097-xxxx", [(1, 1)])
        svc.event_sources.capture("EVT-097-xxxx", "v", "sL", [(1, 1)], [
            {"turn": 1, "role": role, "_source_session_id": "sL", "_source_message_id": seq}
            for role, seq in (("user", "500"), ("assistant", "501"))])
        with patch.object(source_client, "fetch_source_path", return_value=[
            {"message_id": 500, "role": "user", "content": long_text},
            {"message_id": 501, "role": "assistant", "content": long_text}]):
            content = svc.sidecar_call_tool("read_memory_source", {"event_id": "EVT-097-xxxx"})
        check("超长截断并注明", content["status"] == "ok" and content["truncated"]
              and len(content["transcript"]) <= EmberService.READ_SOURCE_MAX_CHARS)
        check("未知 id → not_found", svc.sidecar_call_tool("read_memory_source", {"event_id": "EVT-000-nope"})["status"] == "not_found")



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
        svc = EmberService(store, MockLLMClient(), model="mock")
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

    sig 8-9 + 本轮 ≥2 独立线索 + 语义锚点且分数 ≥ INLINE_SEMANTIC_FLOOR
    （2026-10-19 透传真分数）→ 内联 span 区间（±3 轮扩展）的当时
    对话；任一条件缺失 → 原话行照旧。
    """
    from ember.models import PresentationContext, RecallTrace, RouteHit

    def _hit(cue, matcher="lexical", eid="EVT-097", score=None):
        return RouteHit(route_id=f"rt-{cue}-{matcher}", event_id=eid,
                        cue=cue, matcher=matcher, cue_type="lexical",
                        score=score)

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
        # This test is presentation eligibility, not path transport (covered by
        # test_source_paths and the real App probe). Supply a verified read result.
        svc.read_source_event = lambda eid, **kwargs: {"status": "ok", "transcript": "[turn 5] user: 轮5的原话", "truncated": False}


    def _prefix(svc, anchors):
        ctx = PresentationContext(
            recalled_events=["EVT-097"],
            recall_trace=RecallTrace(input_text="x", anchors=anchors))
        return svc._build_recall_prefix(ctx)

    with tempfile.TemporaryDirectory() as root:
        svc = EmberService(EmberStore(root), MockLLMClient(), model="mock")
        _mk(svc)
        # 正例：双线索（其一语义 0.65 ≥ 0.62 门槛）+ sig 9 → 内联当时对话
        p = _prefix(svc, [_hit("追剧"), _hit("更新", "semantic", score=0.65)])
        check("三条件全满足 → 内联【当时对话】", "【当时对话】" in p, p[:300])
        check("内联含已核验 span 原文",
              "[turn 5] user: 轮5的原话" in p, p[:300])
        check("内联后不再出原话行", "原话：" not in p, p[:300])
        check("内联后无深挖提示", "需要原话时" not in p, p[:300])

        # 反例 1：sig 6（低于 8）
        svc2 = EmberService(EmberStore(root), MockLLMClient(), model="mock")
        _mk(svc2, "EVT-098")
        ev = svc2.store.get_event("EVT-098")
        ev.significance = "6：背景级"
        svc2.store.write_event(ev)
        svc2.turn_spans.fill("EVT-098", [(5, 5)], filled_by="test")
        ctx = PresentationContext(
            recalled_events=["EVT-098"],
            recall_trace=RecallTrace(input_text="x", anchors=[
                _hit("追剧", eid="EVT-098"),
                _hit("更新", "semantic", eid="EVT-098", score=0.65)]))
        p2 = svc2._build_recall_prefix(ctx)
        check("sig 6 → 不内联，走原话行",
              "【当时对话】" not in p2 and "原话：" in p2, p2[:300])

        # 反例 2：单线索（语义 lone hit）
        p3 = _prefix(svc, [_hit("追剧", "semantic", score=0.65)])
        check("单线索 → 不内联",
              "【当时对话】" not in p3 and "原话：" in p3, p3[:300])

        # 反例 3：双线索但全是词面（无语义锚点）
        p4 = _prefix(svc, [_hit("追剧"), _hit("更新")])
        check("无语义锚点 → 不内联",
              "【当时对话】" not in p4 and "原话：" in p4, p4[:300])

        # 反例 4：语义锚点在但分数低于内联门槛（0.58 < 0.62，半相关带）
        p5 = _prefix(svc, [_hit("追剧"),
                           _hit("更新", "semantic", score=0.58)])
        check("语义分低于内联门槛 → 不内联",
              "【当时对话】" not in p5 and "原话：" in p5, p5[:300])


def main():
    test_hooks_do_not_archive_or_call_model()
    test_restart_resume_counter()
    test_key_quotes_validation()
    test_verify_and_presentation()
    test_read_memory_source()
    test_presentation_lines()
    test_inline_source_high_match()
    total = checks
    if failures:
        print(f"\n{total - len(failures)}/{total} checks passed，失败：{failures}")
        sys.exit(1)
    print(f"\n{total}/{total} checks passed")


if __name__ == "__main__":
    main()
