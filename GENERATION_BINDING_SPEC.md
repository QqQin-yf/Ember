# 普通生成绑定与工具回执：本段接口

> 2026-10-24 更新：普通生成部分保持本契约；主动 run 已另行实现独立工作区接入，见 [RUNS_SPEC.md](RUNS_SPEC.md)。下文 unsupported_phase/未实现 runs 的描述保留为此前阶段记录。新 run 首版仅允许 read_memory_source，不能混用 recall_id 或普通 _live_turn；尚未部署日常主动链路。

2026-10-20。本地实现，尚未提交/部署或与真实 app 联调。与 RECALL_REFS_SPEC.md 的呈现/出处段及 RECALL_SNAPSHOT_SPEC.md 的快照段配套；主动 run 仍未实现。

## 1. app 接线所需字段

普通生成首次调用现有 `POST /ember/hooks/turn_start` 时，新增可选 `recall_id`。启用新链路时必须同时提供真实 `session_id`、`message`、`user_message_id`。seq 可以是数字或数字字符串，HTTP 层统一处理；不能把 UUID 填进 seq 字段。旧 context_start_id 仍是 seq。

```json
{
  "session_id":"s1",
  "message":"真实用户消息",
  "user_message_id":"101",
  "recall_id":"r-102"
}
```

首次成功仍返回原有注入文本、turn 和呈现/出处元数据；顶层和 recall_refs 中新增同一 recall_id。现已持久保存原响应，正文保留 24 小时，同 ID 同内容在保留期内重放原响应。可选 occurred_at/activity_revision/window_revision 及查询、pending、过期规则见 RECALL_SNAPSHOT_SPEC.md。

MCP 每次 tools/call 必须逐请求带：

| 请求头 | 内容 |
|---|---|
| X-Heart-Instance | 实例（缺省仍为 default） |
| X-Ember-Recall-Id | 首次登记的 recall_id |
| X-Ember-Tool-Call-Id | app 已持久保存的 tool_use.id |

身份不由模型填入工具参数。每个请求通过 ContextVar 绑定对应实例和普通生成，不修改全局客户端头，不用共享 _live_turn 代答。重复身份头直接 HTTP 400；未知、空或不完整身份不会降级。带主动 X-Ember-Run-Id 返回 unsupported_phase；同时带普通/主动身份返回 context_mismatch，当前没有 run 的可用上下文。

收轮继续使用 `POST /ember/hooks/turn_end`，必须携带同一 recall_id 和 session_id，turn 如提供必须匹配登记值：

```json
{
  "session_id":"s1",
  "recall_id":"r-102",
  "turn":7,
  "assistant_message_id":"102",
  "assistant_reply":"已提交的真实回复",
  "status":"done"
}
```

status 默认为 done，可为 error/cancelled。done 要求真实非空回复和真实 seq；error/cancelled 不把部分输出或空气泡加入语料，不登记虚构回复 seq。完成参数以哈希核对业务一致性，正文不落生成账。重复同一终态返回原回执，改变终态或内容返回 id_conflict。

App d62aad1 / APP_第二阶段回复.md §6 已报告本地接入 turnEnd.recall_id/status 和普通 MCP 两个身份头，假服务验证通过，尚未真实联调。迟到 ready 的持久补收轮仍待 App 接入；明确需要补，规则见 [TURN_END_RECOVERY_SPEC.md](TURN_END_RECOVERY_SPEC.md)。

## 2. 生成生命周期与兼容

每实例一个当前可写的普通生成。首次登记 starting，开轮和登记完成后 running；下一次新的真实生成使旧生成 superseded。正常完成变为 done/error/cancelled；服务重启把未终结生成标 interrupted，不猜测其回复已提交或取消，也不恢复旧写入上下文。

同一用户重新生成可以用新的 recall_id，分配新 Ember turn；旧生成记录、旧 Event、固定来源和已完成调用回执全部保留。Heart 自身的用户刺激幂等规则不变，不能拿新的 Ember turn 要求 Heart 多施加一次普通刺激。

新工具调用只允许 running 且持有本进程对应生成上下文的身份执行。结束/被替代/中断后拒绝新的迟到调用。旧生成已经持久完成的同一调用可返回原回执，这不是重新允许旧生成写入。

迟到的真实收轮可以在 superseded/interrupted 后补记原生成的历史终态/真实 reply 映射，但不往当前会话工作语料追加旧正文，不恢复活动槽，不关掉新生成。已收到终态的重试按原内容幂等；无完成证据的中断不自动伪装 done，也不自动重跑工具。

start 当时超时/pending，之后 refs ready 且 App 已持久保存真实终态时，应补发原轮次收轮；召回正文过期不影响该规则。仍未知且无原成功登记证据时不盲发，详见 TURN_END_RECOVERY_SPEC.md。这里只补历史终态/编号，不重建整轮聊天正文或自动创建 Event。

未传 recall_id 的旧钩子保留兼容行为。新旧模式交替时，新开轮同样使此前的普通生成失去写入资格。当前槽属于新模式时，无身份的 MCP 和无 recall_id 的收轮明确 identity_required；不能靠省略头借用新模式上下文。旧模式本来没有生成级工具幂等，不因此宣称获得此能力。

## 3. 工具幂等与不确定结果

工具操作键为 `(instance, recall_id, tool_call_id)`，同一生成内相同 ID、同工具和同业务参数返回第一次结果；异内容返回 id_conflict。不同生成中的同名 tool_call_id 不互相覆盖或借用回执。JSON 对象键顺序不构成业务变化。

执行前先持久登记 in_progress，再调用工具。完成后保存回执。进程中断、执行抛错或回执保存失败时，操作保持 in_progress/unknown，后续同 ID 不盲重放。Event JSONL 与 SQLite 不是一个原子事务，不能宣称严格 exactly-once：记忆可能已经落库但回执未知，此时应查询/核对实际效果。即使标记 unknown 本身写盘失败，已接受记录也不会被当成未执行。

五个会改变状态的工具（create/revise/search/pin/unpin）保存完成结果；search 的提取记账/恢复 Cue 不会因同调用重试重复执行。验证失败的工具回执也固定；修正参数后要使用新的 tool_call_id，不能拿同 ID 换内容。

read_memory_source 的原文结果只在当前进程内缓存，持久账只保存身份和完成状态，不写 transcript。进程内同调用可返回原结果；重启后返回 result_not_retained，不重新执行旧 ID，也不从当前原文重建旧回执。需要重新读取时由新活动生成明确发起新的调用。此限制避免本段新增原文持久缓存。

来源账区分 writer_recall_id/writer_tool_call_id（哪次调用创建 Event）与逐轮 source_recall_id（已完成实际来源回复所属生成，仅明确收到真实 reply seq 时记录）。创建 Event 的生成不代替多轮历史来源；不解析 r-数字来猜消息身份，不拿预分配 seq 当已有正文。本段没有新增自动记忆 writer 或 pending 写入。

## 4. 查询接口

沿用 Bearer 和实例头：

- `GET /ember/generations/{recall_id}`：生成状态、session/turn、superseded_by、已提交回复 seq、调用列表（tool_call_id/tool_name/state）；不返回请求哈希、用户正文或注入文本。recall_snapshot_available 仅在快照已完成、正文未过期且记账已恢复时为 true；false 不等于无引用元数据。
- `GET /ember/generations/{recall_id}/tools/{tool_call_id}`：对应调用状态和已知回执；旧调用只在所属生成可查。in_progress/unknown 不表示尚未执行。没有保留的读取结果明确 result_not_retained。
- `GET /ember/recalls/{recall_id}`：冻结引用元数据；显式 view=response 重取原响应，见 RECALL_SNAPSHOT_SPEC.md。

生成账路径为每实例根目录的 `generations.sqlite3`，首次新模式登记时才创建；只有标准库 sqlite3，无新依赖。存身份、请求哈希、终态、工具回执，并新增 24 小时召回响应快照；该响应可能包含已注入的有限原文片段，但不额外持久保存用户/回复全文或 read_memory_source transcript。到期清除快照正文，保留引用元数据和防复用身份。身份/回执后续清理另行设计。每实例库由一个服务进程拥有，不支持多个进程写同库。

## 5. 错误与当前边界

普通 HTTP 钩子/查询：无错误 200；unknown_recall/unknown_tool_call 404；invalid_identity/invalid_status/invalid_arguments 400；id_conflict、identity_required、context_mismatch、generation_not_ready 等冲突 409。MCP 调用沿用文本 JSON 业务回执，失败见 status/code；协议/schema 错误仍由 SDK 自己处理。

| code | 含义 |
|---|---|
| recall_snapshot_unavailable | 旧版已登记生成没有快照；不重跑召回或增加 turn |
| recall_in_progress / recall_recovery_pending | HTTP 202，结果计算中或原记账待恢复，不是成功空召回 |
| recall_outcome_unknown | 无已提交快照的中断结果，不盲重跑 |
| response_expired | HTTP 410，24 小时正文已过期，引用仍可查询 |
| stale_generation | 生成已结束、被替代、中断或丢失活动上下文，拒绝新调用 |
| tool_outcome_unknown | 已接受的操作执行中/结果未知，不可盲重放 |
| result_not_retained | 读取回执已完成，但原文不持久保存、当前进程无缓存 |
| id_conflict | 已登记身份的业务内容变了 |
| identity_required / invalid_identity | 必需身份缺失或不合法 |
| context_mismatch | 会话/turn 不匹配，或普通/主动上下文混用 |
| unsupported_phase | 主动 run 绑定尚未实现 |

同 recall_id 同内容的重复 turn_start 在正文保留期内返回原响应，不重新 embedding/选材/记账、增加 turn 或恢复旧槽。计算或恢复中返回 202；正文到期返回 410。旧版仅登记身份而没有快照的记录仍返回 409 recall_snapshot_unavailable，不从当前记忆重建。app 必须区分这些状态，详见 RECALL_SNAPSHOT_SPEC.md。

尚未实现：主动 `/ember/runs/*`、工具结果专用证据、主动 pending 核验、新 `/api/source/path` 客户端迁移和真实三方联调。整个唤醒链路不训练耳朵，召回门槛/选材/注入文本保持原规则。

## 6. 本段验证

含后续快照段共 191 项 unittest 通过，覆盖真实 ASGI/MCP 请求、实例并发、生成替代、重复调用、身份冲突、回执存储失败、重启、迟到终态与 24 小时过期。此前 15 个独立验收入口全部成功退出，其中两项真实数据验收缺 data_real 跳过。未做真实三方联调、推送或部署。
