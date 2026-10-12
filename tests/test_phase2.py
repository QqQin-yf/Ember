# -*- coding: utf-8 -*-
"""Ember v3 Phase 3 — 验收测试。

Phase 3 架构（momo 2026-09-30 22:27 裁决）：
- 记忆写入自然嵌入对话流：模型在对话中自主调用 create_memory_event，
  不再有每轮一次的独立 writer 调用。
- Ember 每轮只做本地 recall（无 LLM），命中则把记忆以结构化前缀块
  挂在用户消息前面。
- 工具调用进历史不变；写作指令由 Ember 注入 system 末尾（wire 层）。

不启 HTTP，直接测 EmberService（proxy.py 是薄封装）。
用 MockLLMClient 模拟"对话+写记忆一体"的模型行为。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ember.llm import MockLLMClient, LLMError
from ember.recall import DirectRecall
from ember.service import EmberService
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


class BrokenLLM(MockLLMClient):
    """T8：带工具的调用故意失败（模拟 provider 故障）。"""

    def chat_completion(self, model, messages, tools=None, **kw):
        if tools and messages and messages[-1].get("role") != "tool":
            text = self._last_user_text(messages)
            if any(t in text for t in self.TRIGGERS):
                raise LLMError("LLM API 人为故障（T8）")
        return super().chat_completion(model, messages, tools=tools, **kw)


def chat(service: EmberService, text: str, session_id: str, model: str | None = None) -> dict:
    payload = {"messages": [{"role": "user", "content": text}], "ember_session": session_id}
    if model:
        payload["model"] = model
    return service.chat_completion(payload)


def main():
    tmp = tempfile.mkdtemp(prefix="ember_p3_test_")
    try:
        store = EmberStore(tmp)
        client = MockLLMClient()
        service = EmberService(store, client, model="mock-chat")

        print("== T0 嵌入式写入：模型自主调用工具 ==")
        r0 = chat(service, "今天我们决定把 Ember 的传播模型改成 Route Search。", "s_t0", model="model-a")
        check("响应带 model 且随请求", r0["model"] == "model-a" and r0["ember"]["model"] == "model-a")
        check("memory created（模型自主触发）", r0["ember"]["memory"]["status"] == "created",
              str(r0["ember"]["memory"]))
        sess = service.sessions["s_t0"]
        roles = [m["role"] for m in sess.messages]
        check("tool_calls 与 tool 消息已进 session 历史",
              "assistant" in roles and "tool" in roles, str(roles))
        tool_msg = [m for m in sess.messages if m["role"] == "tool"]
        check("tool 结果消息存在且含 created 状态",
              len(tool_msg) == 1 and json.loads(tool_msg[0]["content"])["status"] == "created")
        check("写入走主调用+续跑（calls==2）",
              len(client.calls) == 2, f"calls={len(client.calls)}")

        print("== T0b 无写入轮次：一次调用，零额外开销 ==")
        calls_before = len(client.calls)
        r0b = chat(service, "嗯嗯，那接着往下说。", "s_t0", model="model-b")
        check("切换模型后 session 历史延续（turn=2）", r0b["ember"]["turn"] == 2)
        check("闲聊轮也只有一次 LLM 调用", len(client.calls) - calls_before == 1,
              f"delta={len(client.calls) - calls_before}")
        check("闲聊轮 memory skipped", r0b["ember"]["memory"]["status"] == "skipped")
        check("闲聊轮没有工具调用记录", r0b["ember"]["memory"]["tool_called"] is None)

        print("== T1 写入普通事件 ==")
        r = chat(service, "今天我们决定把 Ember 的传播模型改成 Route Search。", "s_t1")
        mem = r["ember"]["memory"]
        check("chat success", r["choices"][0]["message"]["content"] != "")
        check("memory created", mem["status"] == "created", str(mem))
        check("有 event_id", bool(mem["event_id"]))
        check("至少 1 条 route", mem["route_count"] >= 1)
        ev = store.get_event(mem["event_id"])
        check("Event 落库且系统字段由 Ember 生成",
              ev.event_id == mem["event_id"] and ev.version_id.endswith("-v1")
              and ev.raw_ref.startswith("conversation/session-s_t1/turn-"))
        check("LLM 没有提供系统字段",
              not any(k in ev.to_dict() for k in ("activation", "weight")))

        print("== T2 闲聊：不触发工具，不写记忆 ==")
        r2 = chat(service, "今天天气不错。", "s_t2")
        mem2 = r2["ember"]["memory"]
        check("skipped", mem2["status"] == "skipped", str(mem2))
        check("模型没有发出工具调用", mem2["writer_called"] is False)
        check("没有工具调用记录", mem2["tool_called"] is None)

        print("== T3 写作指令注入 wire 层（2026-10-11 无感化：空说明=不注入） ==")
        last_call = client.calls[-1]
        sys_msgs = [m for m in last_call["messages"] if m.get("role") == "system"]
        check("空写作说明时 system 零污染", len(sys_msgs) == 0,
              str([m["content"][:40] for m in last_call["messages"][:2]]))
        # 机制保留验证：填回常量即恢复注入（service 命名空间持有该常量）
        import ember.service as _ember_service
        _old_prompt = _ember_service.EMBEDDED_WRITER_PROMPT
        _ember_service.EMBEDDED_WRITER_PROMPT = "（测试）写作指令标记"
        try:
            chat(service, "再聊一句。", "s_t3b")
            call3 = next(c for c in reversed(client.calls)
                         if c["messages"][-1].get("role") == "user"
                         and c["messages"][-1].get("content") == "再聊一句。")
            sys3 = [m for m in call3["messages"] if m.get("role") == "system"]
            check("填回常量即恢复 system 注入",
                  len(sys3) >= 1 and "（测试）写作指令标记" in sys3[0]["content"],
                  sys3[0]["content"][:60] if sys3 else "无 system")
        finally:
            _ember_service.EMBEDDED_WRITER_PROMPT = _old_prompt

        print("== T4 recall 命中 → 前缀注入用户消息 ==")
        sid = "s_t4"
        r4a = chat(service, "今天我们决定把 Ember 的传播模型改成 Route Search。", sid)
        ev4 = store.get_event(r4a["ember"]["memory"]["event_id"])
        # cue "Ember" 是 must；下一轮提到它应命中
        r4b = chat(service, "我们继续聊 Ember 吧。", sid)
        # 找 r4b 轮的主调用（最后一条 user 消息是"我们继续聊 Ember 吧"的那次）
        main_call = next(c for c in reversed(client.calls)
                         if c["messages"][-1].get("role") == "user"
                         and "我们继续聊 Ember 吧" in (c["messages"][-1].get("content") or ""))
        injected = main_call["messages"][-1]["content"]
        check("命中轮 LLM 收到带前缀的用户消息",
              "[记忆浮现]" in injected and "触点：" in injected, injected[:120])
        last_user_in_hist = [m for m in service.sessions[sid].messages
                             if m["role"] == "user"][-1]
        check("session 历史存用户原话（不存前缀）",
              last_user_in_hist["content"] == "我们继续聊 Ember 吧。",
              repr(last_user_in_hist["content"]))
        check("memory 状态带 recalled_count", r4b["ember"]["memory"]["recalled_count"] >= 1,
              str(r4b["ember"]["memory"]["recalled_count"]))
        _ = r4a, ev4

        print("== T5 写入后 lexical recall 可命中（recall 端点） ==")
        pres = service.recall("Ember")
        check("recall 命中刚写入的 Event",
              any(e["event"]["event_id"] == mem["event_id"]
                  for e in pres["recalled_events"]),
              str([e["event"]["event_id"] for e in pres["recalled_events"]]))
        why = pres["recalled_events"][0]["why_recalled"]
        check("why_recalled 带 route 信息", all("route_id" in w for w in why))

        print("== T6 cue 可观察（无 must/may，momo 2026-10-01 裁决） ==")
        hits = pres["recall_trace"]["initial_anchors"]
        types = {h["match_type"] for h in hits}
        check("cue 命中 match_type 为 lexical", any(t == "lexical" for t in types), str(types))
        pres_b = service.recall("今天我们聊聊 Route Search 的想法")
        b_types = {h["match_type"] for h in pres_b["recall_trace"]["initial_anchors"]}
        check("第二条 cue 命中，trace 可观察",
              any(t == "lexical" for t in b_types), str(b_types))
        check("memory 状态带 recall_cues 列表",
              isinstance(mem["recall_cues"], list) and len(mem["recall_cues"]) >= 1)

        print("== T7 原文可回溯 + Store 重启 ==")
        check("raw_ref 指向原始 turn", ev.raw_ref == f"conversation/session-s_t1/turn-1"
              and ev.turn_refs == ["turn-1"], ev.raw_ref)
        check("写入时 change 为空（留待 Sleep 补写）", ev.change == "", repr(ev.change))
        store2 = EmberStore(tmp)
        service2 = EmberService(store2, MockLLMClient())
        pres2 = service2.recall("Ember")
        check("重启后 Event 仍在", any(
            e["event"]["event_id"] == mem["event_id"] for e in pres2["recalled_events"]))
        check("重启后索引一致", store2.index_snapshot() == store.index_snapshot())

        print("== T8 工具调用失败：如实进历史，chat 仍完成 ==")
        broken_client = BrokenLLM()
        broken = EmberService(EmberStore(tmp), broken_client, model="mock-chat")
        try:
            r8 = chat(broken, "今天我们决定上线新版本。", "s_t8")
            check("chat 整体失败时错误上抛（无隐藏吞没）", False, f"unexpected success: {r8}")
        except LLMError:
            check("chat 整体失败时错误上抛（无隐藏吞没）", True)
        # 失败路径 2：模型发了 tool call 但写入校验失败 → tool result 进历史，chat 继续
        class BadArgsLLM(MockLLMClient):
            def _tool_call_reply(self, text):
                msg = super()._tool_call_reply(text)
                msg["tool_calls"][0]["function"]["arguments"] = '{"event": {"summary": "残缺"}}'
                return msg
        bad = EmberService(EmberStore(tmp), BadArgsLLM(), model="mock-chat")
        r8b = chat(bad, "今天我们决定上线新版本。", "s_t8b")
        mem8 = r8b["ember"]["memory"]
        check("校验失败 chat 仍 success", r8b["choices"][0]["message"]["content"] != "")
        check("memory = failed", mem8["status"] == "failed", str(mem8))
        check("失败结果如实进历史（tool 消息存在）",
              any(m["role"] == "tool" for m in bad.sessions["s_t8b"].messages))
        tool_content = json.loads([m for m in bad.sessions["s_t8b"].messages
                                   if m["role"] == "tool"][0]["content"])
        check("模型能看到失败原因", tool_content["status"] == "failed", str(tool_content))

        print("== T9 多触点共命中聚合（momo 22:52 + 2026-10-01 裁决） ==")
        # 写一条带多个独立触点的 Event（无 must/may 之分）
        class MultiCueLLM(MockLLMClient):
            def _tool_call_reply(self, text):
                msg = super()._tool_call_reply(text)
                msg["tool_calls"][0]["function"]["arguments"] = json.dumps({
                    "event": {
                        "summary": "堂姐国庆在河南结婚",
                        "participants": ["堂姐"],
                        "objects": ["婚礼"],
                        "context": "家庭事务",
                        "core": "我知道了堂姐将在国庆假期在河南举办婚礼。",
                        "outcome": "确认了婚礼安排",
                        "state": "等待国庆假期",
                        "affect": "期待",
                        "significance": "家庭重要事件",
                        "details": [],
                        "key_quotes": [],
                    },
                    "recall_routes": [
                        {"cue_type": "lexical", "cue": "堂姐结婚"},
                        {"cue_type": "lexical", "cue": "堂姐"},
                        {"cue_type": "lexical", "cue": "结婚"},
                        {"cue_type": "lexical", "cue": "国庆"},
                        {"cue_type": "lexical", "cue": "河南"},
                    ],
                    # quote_spans 必填（2026-10-12）：摘本轮用户原话首尾片段
                    "quote_spans": [{"start_quote": text[:8],
                                     "end_quote": text[-8:]}],
                }, ensure_ascii=False)
                return msg
        mc = EmberService(EmberStore(tmp), MultiCueLLM(), model="mock-chat")
        chat(mc, "我决定记下这件事：堂姐结婚，国庆办，在河南。", "s_t9")
        # 没出现完整短语"堂姐结婚"，但多个独立触点同时命中
        pres9 = mc.recall("这次国庆没人结婚。")
        e9 = [e for e in pres9["recalled_events"] if "堂姐" in e["event"]["summary"]]
        check("无组合短语时多触点仍命中",
              len(e9) == 1, str([x["event"]["summary"] for x in pres9["recalled_events"]]))
        if e9:
            check("multi_cue == 2（国庆+结婚）", e9[0]["multi_cue"] == 2,
                  str(e9[0]["multi_cue"]))
            # 前缀块呈现强指向措辞
            ctx9 = mc.store and DirectRecall(mc.store).recall("这次国庆没人结婚。")
            prefix = mc._build_recall_prefix(ctx9)
            check("前缀块标注「多个线索同时指向」", "多个线索同时指向" in prefix, prefix[:200])
        # 单触点 = 弱联想
        pres_single = mc.recall("河南最近怎么样。")
        e_single = [e for e in pres_single["recalled_events"] if "堂姐" in e["event"]["summary"]]
        if e_single:
            check("单触点命中且 multi_cue == 1",
                  e_single[0]["multi_cue"] == 1,
                  str(e_single[0]["multi_cue"]))
            ctx_s = DirectRecall(mc.store).recall("河南最近怎么样。")
            prefix_s = mc._build_recall_prefix(ctx_s)
            check("单触点前缀标注「隐约联想」", "隐约联想" in prefix_s, prefix_s[:200])
        else:
            check("单触点命中", False, "未命中")

        print()
        print(f"结果：{PASS} 通过，{FAIL} 失败")
        if FAIL:
            sys.exit(1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
