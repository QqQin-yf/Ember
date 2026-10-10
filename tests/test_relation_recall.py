# -*- coding: utf-8 -*-
"""Relation 层 v1 测试（momo 2026-10-05 裁决：修正者顶上机制）。

验证点：
1. RelationStore 正确加载 5 条 resolves 边
2. resolvers_of 前缀匹配（完整 id / 短前缀都能查）
3. recall 端点：cue 单命中被排除事件时，修正者被自动带入
4. 修正者已在召回结果中时不重复带入
5. 修正者自身被排除的情况（不存在于当前数据，但逻辑要防）
6. resolved_brought 上报完整
"""
import sys, os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from ember.relation import RelationStore  # noqa: E402

checks = []


def check(name, cond):
    checks.append((name, bool(cond)))
    print(("PASS" if cond else "FAIL"), "-", name)


root = os.path.join(BASE, "data_real")

# 1. 加载
rs = RelationStore(root)
check("5 条 resolves 边已加载", len(rs.all()) == 5)

# 2. 前缀查询
r8 = rs.resolvers_of("EVT-008-15098d")
check("完整 id 查 EVT-008 的修正者 → EVT-009",
      len(r8) == 1 and r8[0].dst_event.startswith("EVT-009"))
r8s = rs.resolvers_of("EVT-008")
check("短前缀同样可查", len(r8s) == 1 and r8s[0].dst_event.startswith("EVT-009"))
check("valid 事件无修正者边", rs.resolvers_of("EVT-009-936f69") == [])

# 3. recall 端点端到端
from ember.store import EmberStore  # noqa: E402
from ember.service import EmberService  # noqa: E402


class _FakeClient:
    def chat_completion(self, **kw):
        raise RuntimeError("test does not call LLM")


store = EmberStore(root)
svc = EmberService(store, _FakeClient(), model="test")

# cue '记忆断层' 命中 EVT-008（superseded 被过滤）→ 修正者 009 应被带入
pres = svc.recall("记忆断层")
injected = {e["event"]["event_id"][:7] for e in pres["recalled_events"]}
brought = pres["resolved_brought"]
check(f"'记忆断层' 带入修正者 EVT-009（实际注入 {sorted(injected)}）",
      "EVT-009" in injected)
# semantic 管线通电后：'记忆断层' 语义命中 010/011 等，008 与 010 双双被过滤、
# 修正者 009/011 已在召回中 → resolved_brought 为空（"已在结果中不算带入"）。
# 不变量断言：superseded（008/010）不出现 + 修正者（009/011）在注入中。
check("superseded 008/010 被过滤（记忆断层查询）",
      "EVT-008" not in injected and "EVT-010" not in injected)
check("修正者顶上生效（009/011 注入中）",
      "EVT-009" in injected and "EVT-011" in injected)
check("被修正者 008 本身未注入", "EVT-008" not in injected)

# '假记忆' 只命中 010 → 011 应带入
pres2 = svc.recall("假记忆那次")
injected2 = {e["event"]["event_id"][:7] for e in pres2["recalled_events"]}
check(f"'假记忆' 带入修正者 EVT-011（实际注入 {sorted(injected2)}）",
      "EVT-011" in injected2)

# 4. Entity 通路：'Ember' 全区域命中，修正者已在 valid 集合中，不重复带入
# （2026-10-10 semantic 管线通电后：模糊查询语义命中大半库，superseded
#   事件也进 excluded→修正者顶上，区域数随数据自然变化——断言改为
#   "superseded 全部被过滤 + 修正者最终在结果中"这一不变量）
pres3 = svc.recall("Ember 最近怎么样了")
brought3 = pres3["resolved_brought"]
injected3 = [e["event"]["event_id"][:7] for e in pres3["recalled_events"]]
check("Entity 通路无重复带入（修正者已在区域内）", len(brought3) == 0)
check("superseded 过滤不变量（008/010 不在注入中）",
      "EVT-008" not in injected3 and "EVT-010" not in injected3)
check("修正者 009/011 在 Entity 区域注入中",
      "EVT-009" in injected3 and "EVT-011" in injected3)

fails = [n for n, ok in checks if not ok]
print(f"\n{len(checks) - len(fails)}/{len(checks)} checks passed")
if fails:
    print("FAILED:", fails)
    sys.exit(1)
