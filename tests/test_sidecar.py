# -*- coding: utf-8 -*-
"""Ember 旁路形态（MCP + hook，2026-10-12 拍板）— 验收测试。

覆盖 service 层的旁路一族（hook_turn_start / hook_turn_end /
sidecar_create_memory_event / sidecar_read_memory_source）与共用抽取
（run_recall_pipeline / write_validated_event / read_source_event）。
MCP 协议层（sidecar.py）是薄翻译层，schema 同源自 writer，不在本文件
展开（依赖 mcp SDK 的用例单独守卫）。

不启 HTTP，直接测 EmberService（与 test_phase2 同款，MockLLMClient
仅满足构造器，旁路路径永远不会调它）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.llm import MockLLMClient
from tests.legacy_event_fixture import LegacyEventService as EmberService
from ember.store import EmberStore

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


_UNSET = object()


def write_args(user_quote: str | None = None,
               cues: tuple[str, ...] = ("Ember",),
               quote_spans: list[dict[str, str]] | None | object = _UNSET,
               extra_ai_quote: str | None = None) -> dict:
    """一份合法的 create_memory_event 参数（字段集与 validate 对齐）。"""
    quotes: list[dict[str, str]] = []
    if user_quote:
        quotes.append({"speaker": "user", "text": user_quote})
    if extra_ai_quote:
        quotes.append({"speaker": "ai", "text": extra_ai_quote})
    args = {
        "event": {
            "summary": "决定把传播模型改成 Route Search",
            "participants": ["ai", "user"],
            "objects": ["Ember"],
            "context": "联调期间的讨论",
            "core": "我们把 Ember 的传播模型改成了 Route Search。",
            "outcome": "架构定型",
            "state": "设计文档已更新",
            "affect": "踏实",
            "significance": "8：影响后续所有记忆链路设计",
            "details": ["细节一"],
            "key_quotes": quotes,
        },
        "recall_routes": [{"cue_type": "lexical", "cue": c} for c in cues],
    }
    if quote_spans is _UNSET:
        # quote_spans 必填（2026-10-12 裁决）：默认给一对可定位的起尾句
        args["quote_spans"] = [
            {"start_quote": "今天我们决定把", "end_quote": "改成 Route Search。"}]
    else:
        # 传 [] 显式模拟「没给」（校验拒绝）
        args["quote_spans"] = quote_spans
    return args


def main():
    global PASS, FAIL
    PASS = FAIL = 0
    # Never inherit a developer/production source URL or send real requests.
    from unittest.mock import patch
    from ember import source_client
    with patch.multiple(source_client, SOURCE_URL="", SOURCE_TOKEN=""), \
         patch.object(source_client, "_open_source", side_effect=AssertionError("Unexpected source network request")):
        return _run_tests()


def _run_tests():
    tmp = tempfile.mkdtemp(prefix="ember_sidecar_test_")
    try:
        store = EmberStore(tmp)
        service = EmberService(store, MockLLMClient(), model="mock-chat")

        print("== S0 轮前钩子：无命中轮不注入 ==")
        r0 = service.hook_turn_start("sc0", "今天天气不错。")
        check("injected=False 且 injected_text=None",
              r0["injected"] is False and r0["injected_text"] is None,
              str(r0)[:120])
        check("turn 从 1 起号", r0["turn"] == 1, str(r0["turn"]))
        check("memory 统计在场", "recalled_count" in r0["memory"])
        e0 = service.hook_turn_end("sc0", "是呀，难得的好天气。")
        check("轮后钩子收轮成功", e0["ok"] is True and e0["turn"] == 1,
              str(e0))

        print("== S1 轮前钩子：heart turn_id 记账映射 ==")
        r1 = service.hook_turn_start("sc1", "今天我们决定把 Ember 的传播模型改成 Route Search。",
                                     heart_turn_id="t-900001")
        check("开轮 turn=1", r1["turn"] == 1)
        check("映射账本记下 heart 钉子",
              service.turn_ledger.heart_id_of("sc1", 1) == "t-900001",
              str(service.turn_ledger.mapping))

        print("== S2 MCP 写入工具：落库 + 回执 + 出处自动关联 ==")
        receipt = service.sidecar_create_memory_event(write_args(
            user_quote="决定把 Ember 的传播模型改成",
            quote_spans=[{"start_quote": "今天我们决定把",
                          "end_quote": "改成 Route Search。"}]))
        check("回执 status=created", receipt.get("status") == "created",
              str(receipt)[:200])
        check("有 event_id 且落库",
              bool(receipt.get("event_id"))
              and store.get_event(receipt["event_id"]).event_id
              == receipt["event_id"])
        check("出处指向 本会话/本轮",
              receipt.get("raw_ref") == "conversation/session-sc1/turn-1",
              receipt.get("raw_ref", ""))
        check("至少 1 条 route", receipt.get("route_count", 0) >= 1)
        ev = store.get_event(receipt["event_id"])
        check("heart 钉子可从账本换算（梦通道地基）",
              service.turn_ledger.heart_id_of("sc1", 1) == "t-900001")

        print("== S3 key_quotes 逐字核对：真话留下，编造丢弃 ==")
        check("真话保留", any(q.get("text") == "决定把 Ember 的传播模型改成"
                              for q in ev.key_quotes), str(ev.key_quotes))
        r3 = service.sidecar_create_memory_event(write_args(
            user_quote="决定把 Ember 的传播模型改成",
            extra_ai_quote="这句从来没说过"))
        check("编造句进 dropped_quotes",
              len(r3.get("dropped_quotes", [])) == 1,
              str(r3.get("dropped_quotes")))

        print("== S4 轮后钩子：原文存档 + 翻原文读回 ==")
        reply1 = "好的，这个决定我记下了。"
        e4 = service.hook_turn_end("sc1", reply1)
        check("收轮成功", e4["ok"] is True and e4["turn"] == 1, str(e4))
        src = service.sidecar_read_memory_source(receipt["event_id"])
        check("旧无编号出处不冒充可读原文", src.get("status") == "not_archived", str(src)[:160])
        check("不拿当前内存补旧出处", "transcript" not in src)

        print("== S5 收轮后无进行中轮：写入如实回错 ==")
        r5 = service.sidecar_create_memory_event(write_args())
        check("failed 且给出原因",
              r5.get("status") == "failed" and "轮前钩子" in r5.get("error", ""),
              str(r5)[:160])

        print("== S6 再次召回：Cue 命中 → 注入文本可交付 ==")
        r6 = service.hook_turn_start("sc1", "我们继续聊 Ember 吧。")
        check("injected=True", r6["injected"] is True, str(r6)[:120])
        check("注入文本带 [记忆浮现] 块",
              "[记忆浮现]" in (r6["injected_text"] or ""),
              (r6["injected_text"] or "")[:80])
        service.hook_turn_end("sc1", "好呀，你想从哪说起？")

        print("== S7 校验失败如实回执 ==")
        service.hook_turn_start("sc7", "开一轮再写。")
        r7 = service.sidecar_create_memory_event({"event": {"summary": "残缺"}})
        check("failed 且带 validation 原因",
              r7.get("status") == "failed"
              and "validation failed" in r7.get("error", ""),
              str(r7)[:160])
        service.hook_turn_end("sc7", "好。")

        print("== S8 会话错配与串轮竞态守卫 ==")
        service.hook_turn_start("sc8", "开一轮。")
        e8 = service.hook_turn_end("别的会话", "回复")
        check("session_id 不匹配拒绝收轮",
              e8["ok"] is False and "sc8" in e8.get("reason", ""), str(e8))
        r8b = service.hook_turn_start("sc8", "连发第二条（覆盖活跃轮）。")
        e8c = service.hook_turn_end("sc8", "慢到的旧轮回报", turn=r8b["turn"] - 1)
        check("stale turn_end 被拒收",
              e8c["ok"] is False and "stale" in e8c.get("reason", ""), str(e8c))
        e8d = service.hook_turn_end("sc8", "当前轮正常收轮", turn=r8b["turn"])
        check("当前轮号正常收轮", e8d["ok"] is True and e8d["turn"] == r8b["turn"],
              str(e8d))

        print("== S9 重启接续：turn 计数从存档恢复 ==")
        service2 = EmberService(EmberStore(tmp), MockLLMClient(), model="mock-chat")
        r9 = service2.hook_turn_start("sc1", "重启后再聊一句。")
        check("turn 接续（前值 2 + 1）", r9["turn"] == 3, str(r9["turn"]))
        check("映射账本重启后在位",
              service2.turn_ledger.heart_id_of("sc1", 1) == "t-900001")

        print("== S10 工具清单与代理形态同源 ==")
        names = {s["function"]["name"] for s in service2.sidecar_tool_specs()}
        check("六件工具齐全（检索/修正/钉住）",
              names == {"create_memory_event", "read_memory_source",
                        "revise_memory_event", "search_memory",
                        "pin_memory", "unpin_memory"},
              str(names))

        print("== S11 代理形态回归：chat 流程仍走同一召回/写入实现 ==")
        r11 = service2.chat_completion({
            "messages": [{"role": "user",
                          "content": "今天我们决定把 Ember 的传播模型改成 Route Search。"}],
            "ember_session": "s_regress"})
        check("chat 仍正常出回复", r11["choices"][0]["message"]["content"] != "")
        check("chat 内写入仍 created",
              r11["ember"]["memory"]["status"] == "created",
              str(r11["ember"]["memory"])[:160])
        r11b = service2.chat_completion({
            "messages": [{"role": "user", "content": "我们继续聊 Ember 吧。"}],
            "ember_session": "s_regress"})
        check("chat 召回仍注入（共用流水线）",
              r11b["ember"]["memory"]["recalled_count"] >= 1,
              str(r11b["ember"]["memory"])[:160])

        print("== S12 跨重启语料（去原文化 2026-10-14）：未配置拉取 → 如实拒绝 ==")
        # sc1 的 turn-1 原话已不在本地（transcripts 退役，原文只存后端）；
        # service2 是重启后新实例（内存为空），EMBER_SOURCE_URL 未配置 →
        # 拉不到 → 引用更早轮原话如实核对失败（绝不编造）
        service2.hook_turn_start("sc1", "重启后再写一条。")
        r12 = service2.sidecar_create_memory_event(write_args(
            user_quote="今天我们决定把 Ember 的传播模型改成",
            quote_spans=[{"start_quote": "今天我们决定把",
                          "end_quote": "改成 Route Search。"}]))
        check("拉取未配置 → 跨重启核对如实拒绝",
              r12.get("status") == "failed", str(r12)[:200])
        service2.hook_turn_end("sc1", "好，这条也记下了。")

        print("== S12b 跨重启语料：配置原文接口后核对通过 ==")
        # 账本补记 sc1 turn-1 的 message_id 映射 + mock 原文接口返回当轮原文
        from ember import source_client as sc
        service2.turn_ledger.record("sc1", 1, None, user_message_id=901)
        service2.turn_ledger.record_reply("sc1", 1, assistant_message_id=902)
        from unittest.mock import patch
        proof = patch("ember.service.generation_messages", return_value=[
            {"turn": 1, "role": role, "message_id": seq}
            for role, seq in (("user", "901"), ("assistant", "902"))])
        proof.start()
        orig_url, orig_fetch = sc.SOURCE_URL, sc.fetch_source_path
        sc.SOURCE_URL = "http://source-mock"
        sc.fetch_source_path = lambda sid, ids, **kw: [
            {"message_id": 901, "role": "user",
             "content": "今天我们决定把 Ember 的传播模型改成 Route Search。"},
            {"message_id": 902, "role": "assistant",
             "content": "好的，这个决定我记下了。"},
        ]
        try:
            service2.hook_turn_start("sc1", "重启后再写一条（已配置拉取）。")
            r12b = service2.sidecar_create_memory_event(write_args(
                user_quote="今天我们决定把 Ember 的传播模型改成",
                quote_spans=[{"start_quote": "今天我们决定把",
                              "end_quote": "改成 Route Search。"}]))
        finally:
            sc.SOURCE_URL, sc.fetch_source_path = orig_url, orig_fetch
            proof.stop()
        check("拉取语料跨重启核对通过",
              r12b.get("status") == "created"
              and not r12b.get("dropped_quotes"), str(r12b.get("dropped_quotes")))
        ev12 = service2.store.get_event(r12b["event_id"])
        span12 = service2.turn_spans.span_of_event(ev12)
        check("拉取句跨重启精确定位（span 含 turn 1）",
              any(s <= 1 <= e for s, e in span12), str(span12))
        service2.hook_turn_end("sc1", "好，这条也记下了。")

        print("== S13 历史原文未配置：拒绝且不误报摘录错误 ==")
        n_events_before = len(service2.store.all_events())
        service2.hook_turn_start("sc1", "再写一条。")
        r13 = service2.sidecar_create_memory_event(write_args(
            quote_spans=[{"start_quote": "这句话不存在111",
                          "end_quote": "这句话不存在222"}]))
        check("原文未配置 → source_unconfigured",
              r13.get("status") == "failed"
              and r13.get("code") == "source_unconfigured",
              str(r13)[:160])
        check("拒绝发生在落盘前（库中没有半成品）",
              len(service2.store.all_events()) == n_events_before)
        # 「没给」也封死：省略/空 quote_spans → 校验拒绝（quote_spans 必填）
        r13b = service2.sidecar_create_memory_event(write_args(quote_spans=[]))
        check("没给句对 → 校验拒绝（必填封死省略路径）",
              r13b.get("status") == "failed"
              and "quote_spans" in r13b.get("error", ""),
              str(r13b)[:160])
        service2.hook_turn_end("sc1", "好。")

        print("== S14 原文可读但摘录不存在：拒绝且不落盘 ==")
        isolated = EmberService(EmberStore(os.path.join(tmp, "quote-only")), MockLLMClient())
        isolated.hook_turn_start("quote-only", "过去真实原文。", user_message_id=1)
        isolated.hook_turn_end("quote-only", "过去真实回复。", assistant_message_id=2)
        isolated.hook_turn_start("quote-only", "当前真实原文。", user_message_id=3)
        rows = {1: {"role": "user", "content": "过去真实原文。"},
                2: {"role": "assistant", "content": "过去真实回复。"},
                3: {"role": "user", "content": "当前真实原文。"}}
        def exact_source(sid, ids, **kwargs):
            return [dict(message_id=int(seq), **rows[int(seq)]) for seq in sorted(ids, key=int)]
        with patch.object(sc, "SOURCE_URL", "http://source-mock"), \
             patch.object(sc, "fetch_source_path", side_effect=exact_source) as provider:
            missing = isolated.sidecar_create_memory_event(write_args(
                quote_spans=[{"start_quote": "不存在的起句", "end_quote": "不存在的尾句"}]))
        check("通过精确路径读取了原文", provider.call_count == 1)
        check("可读原文中未找到摘录 → quote_not_found",
              missing.get("status") == "failed" and missing.get("code") == "quote_not_found",
              str(missing))
        check("错误摘录不创建事件", not isolated.store.all_events())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n==== sidecar 验收：PASS={PASS} FAIL={FAIL} ====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
