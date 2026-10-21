# Ember 呈现引用与固定来源账：本段实现

2026-10-20。对齐 Heart 三方契约 v0.6 中可独立实施的呈现元数据/出处辅助部分。仅本地实现与隔离测试，不等于完整第二阶段或生产部署完成。

## 1. 已实现与明确未实现

已实现：现有 `/ember/hooks/turn_start` 响应新增 `presentations` 与 `recall_refs`；新实时事件写入时保存独立的 `event_sources.jsonl` 来源账。

原有 injected_text、memory 统计、门槛、席位、轻量续注、选材和 `/api/source` 消费路径保持原行为。新字段不参与召回筛选，缺映射不使聊天失败。没有接入 Heart 推送、ODE、学习或日常调度。

后续已实现普通生成绑定和稳定 recall_id 快照、in_progress/查询/恢复及用户确认的 24 小时正文 TTL，见 GENERATION_BINDING_SPEC.md 和 RECALL_SNAPSHOT_SPEC.md。不带 recall_id 的旧钩子重试仍会开新 turn。主动 run/pending 和新原文读取客户端未实现，未改 app 或 Heart 仓库，尚未切换 `/api/source/path` 或完成三方联调。

## 2. 轮前响应的新增字段

```json
{
  "presentations": [
    {"presentation":"entity_overview","entity_id":"ent-project"},
    {"presentation":"event_text","event_id":"EVT-example","version_id":"EVT-example-v1"}
  ],
  "recall_refs": {
    "session_id":"trigger-session",
    "anchor_message_id":"101",
    "events":[{
      "event_id":"EVT-example",
      "version_id":"EVT-example-v1",
      "presentation":"event_text",
      "source_session_id":"source-session",
      "source_turn_spans":[[3,4]],
      "turn_messages":[
        {"turn":3,"user_message_id":"71","assistant_message_id":"72"},
        {"turn":4,"user_message_id":"75","assistant_message_id":"76"}
      ],
      "source_evidence":"frozen_messages",
      "resolution":"unresolved",
      "reason":"source_path_unknown",
      "source_leaf_id":null,
      "message_ids":null
    }]
  }
}
```

示例仅说明字段；编号为全局 seq 字符串，不是 UUID。anchor_message_id 缺失时为 null。Event version_id 使用当时原值字符串，未知 null，不生成版本。旧模式响应不含 recall_id；新模式按该 ID 保存 24 小时原响应快照，并可固定请求提供的原始活动/窗口版本，见 RECALL_SNAPSHOT_SPEC.md。这不等于主动链路或三方联调完成，不能直接启用调度。

`presentations` 按渲染顺序记录：

| presentation | 实际内容 | events 引用资格 |
|---|---|---|
| event_text | 实际输出事件 core/summary 的正文（含修正承接） | 有 |
| event_excerpt | 实际输出某事件摘要、原话或观察信息点 | 有 |
| entity_overview | 实体说明、成员 ID 清单或仅报实体名/经历数量 | 无隐藏成员资格 |
| retrieval_hint | 未展示条数提示、没有事件内容的检索指引 | 无 |

轻量条目可带 `light:true`。轻量散事件确实输出其 summary 时是 event_excerpt；只说实体名下相关经历时是 entity_overview，即使文本中出现了事件 ID 也不取得资格。实体块同时含实体说明和逐条正文时，两部分分别分类。空正文且只有可核验原话的事件归 event_excerpt；无内容则不输出事件引用。

`recall_refs.events` 只收实际输出事件内容的前两类，按事件去重保序；同一事件同时出现时正文优先。不会从候选列表、落选者、实体全体成员、修正承接的旧认知背景中额外扩充。只有概览时 events=[]，但 presentations 仍保留概览；没有任何召回则两者都是空数组。HTTP 非法/空输入仍沿用旧回执，不冒充一次成功空召回。

## 3. 来源账：固定已知身份，不猜缺失的那半轮

每实例库新增 `event_sources.jsonl`，只保存事件身份、初始内容版本、来源会话、turn spans、逐轮 user/assistant seq 及身份可信标记；没有聊天正文、提醒、原文副本或 injected_text。来源不写入 Event 的认知字段。

写入引用句仍按原规则核验；核验通过后，从同一份定位语料固定来源身份。只有普通钩子随真实消息携带的明确 seq 可作本段的可信绑定，不能从旧 turn_ledger 的最大 reply、数值 min/max、当前 leaf 或 Event 的出生 turn 推断。首次记录固定，后来收轮、更大回复号、修改 turn spans 或重启都不改写它。

如果调用记忆工具时本轮 assistant 还没产生真实 seq，来源账如实固定 null。后来 turn_end 带来的回复不追写成当时证据。需要补齐/验证的路径属于后续独立解析/证据协议，本段没有自动补全，更没有新增自动写记忆。

原文查询仍用旧接口，因此通过旧 `/api/source` 拉来的语料不能证明历史路径身份；代理入口没有 seq 的语料亦如此。旧事件不自动回填。如果选中的语料含无法绑定或冲突的消息，则不把看似可信的起尾编号继续交给 app 解析，以免端点走通掩盖中间语料归属不明；outgoing turn_messages=[]，已知辅助资料仍在来源账保留。

`source_evidence`：

- frozen_messages：新来源账固定了已知消息身份；仍可能有缺失编号。
- legacy_unverified：旧事件无新来源账；不从可变旧账生成 turn_messages。
- unverified_corpus：新写入的定位语料含未知/冲突身份；对外不给可误用的端点。

Ember 不持有 parent_id 树，本段一律不自行返回 resolved 或 source_leaf_id。缺会话用 source_session_unknown，缺 span 用 source_span_missing，可信但缺编号用 source_seq_missing，旧/不可证明的来源或尚待 app 核验的路径用 source_path_unknown；message_ids=null，不能用 [] 假装成功。

app 可把可用的 source_session_id + source_turn_spans + turn_messages 交给已实现的 resolveRecallSource。它校验完整父链，展开非连续段内的有序 seq；保留旧分支可作为历史来源。遗留证据不足保持 unresolved，不能因最新最大回复能走通就认定来源正确。Ember 的 unresolved 是待 app 解析的原始输出，app 维护解析结果，不改写 Ember 来源账。

来源记录先于 Event 保存；中断可能留下一条没有 Event 的来源记录，不主动创建 Event，也不将此当作跨文件事务或记忆工具幂等保证。同一实例由现有单进程服务使用；本段没有新增多进程写同一库的支持。

## 4. 后续交接与验证边界

app 原文解析/读取的新实现保持现状，旧 `/api/source` 暂不改。待 Ember 的新路径读取客户端完成并覆盖翻原文、写入核对、sleep 等消费方后，再协调迁移，不提前收紧旧接口。

普通生成显式工具绑定和稳定召回快照已本地实现，见 GENERATION_BINDING_SPEC.md 与 RECALL_SNAPSHOT_SPEC.md。后续独立工作仍包括 run/tool 专用证据、pending 查询和完成、原文客户端迁移及三方联调。pending 仅针对主动轮中 AI 已显式调用的记忆工具；没有调用就不生成 pending 或长期 Event。整个唤醒链路不训练 Heart 耳朵。

隔离测试覆盖：正文/摘录/概览区分、隐藏和落选成员排除、轻量/修正/观察呈现、原文字串兼容、实际轮前响应、来源冻结、重启与最大 reply 不改来源、无编号和旧语料、跨会话/实例、缺版本、无内容事件及引用失败无半成品。

本次验证结果：136 项 unittest 通过；独立验收脚本全部成功退出（两项真实数据验收缺数据跳过）；19 个旧版/新版渲染对比场景逐字一致。未做真实三方联调或生产部署。
