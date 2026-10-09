# -*- coding: utf-8 -*-
"""Sleep 过滤通电测试（momo 2026-10-04 裁决的验证）。

裁决：superseded/outdated 不参与自动召回，但保留在库里。
验证点：
1. SleepStateStore 正确加载旁路文件，5 条 superseded 被识别
2. recall 端点：cue 命中被排除事件时不注入（计数上报）
3. Entity 通路同样被过滤
4. 显式检索（view_event）不受影响——排除只是召回资格，不是删除
5. 未评估的事件（无 sleep 记录）默认 valid 不误伤
"""
import sys, os, json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember.sleep_state import SleepStateStore  # noqa: E402

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


root = os.path.join(BASE, "data_real")

# 1. 加载
ss = SleepStateStore(root)
check("旁路文件已加载", len(ss.event_status) > 0)
excluded = set(ss.excluded_event_ids())
check(f"识别出 5 条排除事件（实际 {len(excluded)}）", len(excluded) == 5)

# 2. 按前缀查询（sleep_state 里存完整 id）
check("EVT-008 前缀查询为 superseded", ss.status_of("EVT-008-15098d") == "superseded")
check("EVT-010 为 superseded", ss.status_of("EVT-010-bc8bdd") == "superseded")
check("EVT-009 仍 valid", ss.status_of("EVT-009-936f69") == "valid")
check("不存在的事件默认 valid（不误伤）", ss.status_of("EVT-999-xxxxxx") == "valid")

# 3. Service 层过滤（不启动 HTTP，直接构造 service）
from ember.store import EmberStore  # noqa: E402
from ember.service import EmberService  # noqa: E402


class _FakeClient:
    def chat_completion(self, **kw):  # 不真正调 LLM
        raise RuntimeError("test does not call LLM")


store = EmberStore(root)
svc = EmberService(store, _FakeClient(), model="test")

# 4. recall 端点：找一个只命中被排除事件的 cue
# EVT-008 的 route cue 里应有与"断层"相关的；EVT-009 是 valid 且与链路相关
pres = svc.recall("记忆断层")
excluded_ids = set(pres["filtered_by_sleep"]["event_ids"])
injected = {e["event"]["event_id"] for e in pres["recalled_events"]}
overlap = {e for e in excluded_ids if e in injected}
check(f"superseded 事件未注入 recall 结果（排除 {len(excluded_ids)} 条，"
      f"泄漏 {len(overlap)}）", len(overlap) == 0)
check("过滤计数已上报", pres["filtered_by_sleep"]["count"] >= 1)

# 5. Entity 通路同样过滤："Ember" 命中整个区域，应排除 5 条后注入
pres2 = svc.recall("Ember 最近怎么样了")
excluded2 = set(pres2["filtered_by_sleep"]["event_ids"])
injected2 = {e["event"]["event_id"] for e in pres2["recalled_events"]}
overlap2 = {e for e in excluded2 if e in injected2}
check(f"Entity 通路同样被过滤（泄漏 {len(overlap2)}）", len(overlap2) == 0)
check("Entity 区域 valid 事件仍正常注入", len(injected2) >= 5)

# 6. 显式检索不受影响：view_event 对被排除事件仍可用
ev = svc.view_event("EVT-008-15098d")
check("显式检索 superseded 事件仍可达（保留非删除）", ev is not None)

fails = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(fails)}/{len(checks)} checks passed")
if fails:
    print("FAILED:", fails)
    sys.exit(1)
