# 原来源轮完成后的出处补交协议

2026-10-21 验收补充：App a7d82f3 / §11 已报告真实 resolved、迟到 resolution 与三方幂等通过；本方逐项核对及仍缺的故障场景见 SOURCE_COMPLETION_COVERAGE.md。完整历史轮上的 source_recall_id 只是身份标记，不要求每轮都有补交。下文“App 仍需补验”为交付时点记录，最新状态以本段和覆盖表为准。

2026-10-21。已读 `D:/deliz/APP_第二阶段回复.md` §9，按用户确认实现同生成 done 后的独立来源绑定、source_candidates 与 sources_revision 更新。本文为已实现契约；交付代码及隔离服务加载版本见本次提交和 D:/phase2-secrets/ember-readiness.json。App 仍需接纳新证据并补验真实出处解析及 Heart resolution，不能把 Ember 自测当成这两项已经通过。

## 1. 为什么能补，补的是什么

记忆工具在最终回复提交前执行，来源语料可能只有该轮真实用户消息。此时缺 assistant seq 是如实冻结的状态。后续 done 收轮能证明原生成实际提交了哪条回复，因而可以补齐该来源轮的闭合路径。

这份新证据只证明“这个来源轮后来由这条真实回复完成”，**不证明该回复文本在写记忆时已经存在、被 quote_spans 引用或参与了原核验**。原 key_quotes/quote_spans 的逐字核验不变，不给原本不存在的引用补证，不新增 Event、不重写其内容或来源跨度。

不能只拿 writer_recall_id 填整个事件的所有来源轮。多轮事件只补已绑定到该生成的那一轮；其他轮缺证据仍 unresolved。以后可支持 user-only 的精确引用，但不在本方案里改变 App 现有按轮取闭合路径的规则。

## 2. Ember 负责核验和持久补交

适用条件必须全部满足：

1. 原记忆确由已登记普通生成的显式 create_memory_event 创建；有 writer_recall_id、writer_tool_call_id、已完成且返回该 event_id 的工具回执，以及不可变原出处记录。
2. 待补的具体来源轮属于同一实例、同一 source_session_id，原出处跨度已包含该轮，user seq 已在写入时核验。来源轮必须精确匹配该生成的原 turn、真实用户 anchor；不得只看“创建发生在这一轮”。
3. 新写入的出处 schema_version=2，在 pending_sources 中另记该缺失位置的 turn、user_message_id、pending_source_recall_id，只标记确属写入生成且无已存在 assistant 语料的缺失位置；不靠当前 live 槽或以后变大的 reply 倒推。writer 身份与逐轮 source 身份继续分开。
4. 收到同 recall_id、同 session/turn 的 done，含真实非空最终正文和真实 assistant seq，沿用现有收轮业务哈希/冲突校验。只在这些条件成立后，把真实 seq 记入独立的 completion binding。
5. 已有确切 assistant seq 不覆盖；重复相同绑定不产生新证据或修订。已有终态或编号冲突须对账，不以新编号替换。error/cancelled、unknown、无终态或身份不明不补。

补交记录绑定原出处记录的稳定标识/哈希及原内容身份，而非查询时的当前 Event。原 event_sources 首条记录保留；独立旁路账只保存身份、seq、证据类型与操作标识，不另存完整回复正文。即使 Event 后来 revise，也不能从最新内容重建旧召回的出处。

对于现有 156f29e 创建的部分出处，可以在原工具完成回执、原生成/用户锚点、原冻结来源轮与已完成 done 均可逐项核对时建立显式兼容补交；缺任一证明则保留 unresolved。不对 legacy 无身份写入、历史最大 reply 或不可信语料做批量回填。App §9 的具体测试事件须按这些条件核对，不预先宣称全部可修复。

## 3. 快照与来源修订

- 原 snapshot、recall_refs、presentations、首次完整 response 和 App/Heart revision=0 outbox 永远不改；同 ID start 仍返回原响应。
- 某 recall 的冻结事件列表已含该事件，且补交能与该条目原内容/出处身份严格对应时，仅更新独立 source_candidates 视图。事件顺序、event_id、version_id、presentation、原 session/spans 及其他已知 seq 不变，只补该缺失位置和明确证据说明；不新增隐藏成员。
- 该 recall 的 sources_revision 在可用候选实际变化时持久 +1。一次有效更新只推进一次；同 done 重试、查询、重启恢复不推进。同一证据影响多个既有 recall 时，各自维护修订，不共享计数。
- 一次 done 补交与受影响视图的更新需要事务或可恢复操作账。崩溃后从同一证据继续，不发布“新修订配旧候选”的半成品；未完成的工作可幂等恢复，不能只在内存回调里补。
- 补交在某次新召回创建前已完成：新 snapshot 可以冻结当时已有的完成绑定，初始 sources_revision 为 0。不能给一个尚未创建的 recall 虚构历史修订。
- 24 小时正文 TTL 不变；正文过期但 refs 保留时仍可提供来源修订，不重新保存或延长 injected_text，不刷新召回/提取时钟。

因此，**更新的是“实际包含该事件的召回”，不一定是“写记忆那次生成的 recall_id”**。写入发生在其开轮快照之后，该快照通常根本不含这个新事件；不能为了补交把新事件塞进写入轮的旧快照。

## 4. 给 App 的证据形状

原本在写入时已具备完整消息身份的条目继续使用 source_evidence=frozen_messages。经过本次独立补交的条目使用新增类型 **generation_completed**，不把后来证据重新命名成写入时冻结的原文。

source_candidates 中单个条目的补交字段示意（其余原字段保持）：

```json
{
  "source_evidence": "generation_completed",
  "turn_messages": [{
    "turn": 7,
    "user_message_id": "101",
    "assistant_message_id": "102",
    "source_recall_id": "r-install-original"
  }],
  "completion_bindings": [{
    "turn": 7,
    "recall_id": "r-install-original",
    "user_message_id": "101",
    "assistant_message_id": "102",
    "evidence": "turn_end",
    "status": "done"
  }],
  "resolution": "unresolved",
  "reason": "source_path_unknown"
}
```

App 必须给 RefEvent 增加相应类型/字段支持。接纳 generation_completed 时核对补交与来源轮的身份/编号一致，再按真实消息库、原会话与父链验证；不能只加一个白名单字符串就直接判 resolved。Ember 仍不替 App 证明原文树路径。中间轮缺所需编号、未绑定的来源、跨会话/兄弟分支等继续 unresolved。混合多轮条目中，未补部分仍依赖原 frozen evidence，不因一项补交而全部升级可信。

turn_messages、source_evidence、completion_bindings 仍仅限 Ember→App 核验，不透传 Heart。Heart 继续收现有白名单，不需要新增接口。

## 5. App 负责迟到解析与 Heart 修订

App 继续是唯一触发方：查询发现 sources_revision 增加，核验 source_candidates，调用自己的解析函数。只有解析结果实际变化且原初始登记已被 Heart 持久接受，才发新操作身份的 resolution；不修改初始包、不新增成员/版本。

sources_revision 与 Heart resolution_revision 各自独立：新证据仍不能解析时，不为形式增加 Heart 修订；原文服务修好而 Ember 修订未变时，App 也可用已有可信候选重试。旧引用包初始为 unresolved，不能因补交而悄悄改写同一幂等请求。

Ember 不直推 Heart，不重新召回/注入、不新增刺激、不复活周期、不训练耳朵、不自动写新记忆。旧 /api/source/path 客户端迁移、原文提供方未配置、会话删除通知仍是单列事项，不因本方案而宣称解决。

## 6. 下一段验收

Ember 已实现独立补交与可恢复修订；App 新证据核验仍由 App 接线。隔离验收覆盖：

1. 工具写入→同生成 done→后续新召回：可用真实消息树解析。
2. 写入→尚未 done 时被另一轮召回→done：原 snapshot 不变，候选补齐、sources_revision +1，App 独立 resolution 成功。
3. 同 done 重试、补交中崩溃/重启恢复：无重复推进，不重跑工具。
4. 已替代生成的历史 done、正文过期后补交：不影响当前生成，仍可更新匹配的来源辅助。
5. cancelled/error、跨实例/会话/生成、历史不可信编号、多轮缺证据、改变过内容/来源身份：不冒认来源，保持 unresolved 或冲突。

新增 source_completions 与 recall_sources 两个 SQLite 旁路表。完成绑定按原出处哈希和来源 turn 唯一；绑定与受影响候选/修订在同一事务发布。原工具完成回执、原开轮元数据及不可变 done 回执共同充当持久恢复依据，不另存回复全文。启动、查询、开轮及重复收轮会恢复；done 已提交而补交事务暂时失败时，done ACK 仍有效，refs 查询在恢复失败期间返回 202 recall_recovery_pending，不伪报已更新。

213 项 unittest 通过，其中新增 22 项覆盖精确绑定、旧版兼容、错误身份、工具回执未知、事件身份不匹配、多轮缺证据、终态冲突、过期、事务回滚和重启恢复。真实 HTTP/MCP 探针为 scripts/check_source_completion_http.py；现场结果与加载版本写入隔离就绪回执。App §9 通过项仍按其原报告记录，本段不重跑 App/Heart，不新增 backfill 协议，不启用生产开关或唤醒调度。
