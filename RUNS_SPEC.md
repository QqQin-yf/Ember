# Ember 主动 run 首版契约：ember-runs/1

2026-10-24。用于 App `wake-integration` worktree 接入；本次只在 Ember 工作区实现及隔离测试，不部署日常、不修改 8790、不启用主动发送。普通对话的 recall_id/turn_start/turn_end 保持独立。

## 通用要求

沿用 Ember Bearer 认证和逐请求 `X-Heart-Instance`，每个实例一份 `runs.sqlite3`，不存在跨实例回退。运行身份由 App 传递，不进模型提示、不用普通 `_live_turn`，不造 user/assistant 消息、turn、自动召回或长期 Event。

POST 均是严格 JSON 对象；必填键即使值为 null 也须给出。未声明字段（包括提醒/正文的 start 字段、伪 branch_id）拒绝。标识符为非空、不带首尾空白、最长 512 字符的字符串。消息号为正整数或十进制字符串，拒绝布尔值与重复编号；数组顺序照录，不按 seq 推测路径。

## POST /ember/runs/start

必填字段与 App 当前实际发送一致：

| 字段 | 类型与含义 |
|---|---|
| event_id | 固定 start 操作身份 |
| kind | 固定 wake |
| run_id / wake_id / session_id | 独立运行、候选、真实会话身份 |
| activity_revision / window_revision | 开始版本，非负安全整数；不随重试变化 |
| leaf_id / context_start_id | 真实消息 UUID 或显式 null；不透明引用，不能拿 seq 或虚构 main 代替 |
| messages | 最多 10000 个 `{message_id, role}`；role 仅 user/assistant，不含正文/reminder |
| occurred_at | 固定 ISO 8601 带时区时间 |

返回 200，至少含 `protocol_version=ember-runs/1`、event_id、kind=wake、origin=assistant_initiated、run_id、wake_id、session_id、status=running、开始版本/不透明路径/occurred_at、`allowed_tools=[read_memory_source]`、`memory_writes=false`、`window_evidence=app_attested`。

开始窗口是可信 App 的身份快照，不冒称 Ember 已访问原文证明它；不因此取得长期记忆写入权限。App 负责开始前、执行中和提交前的版本/路径、用户优先和取消校验。服务不复制 Heart 的候选确认职责。

同一 event_id 同内容重放原响应，包括运行后来终结的情况，不恢复执行上下文；event_id 内容冲突、同 run/wake 换 start 操作均 409 id_conflict。每实例仅一个 running/finishing 槽，未解决的旧任务不能被新 run 覆盖（409 run_busy）。

## POST /ember/runs/finish

必填：`event_id, kind, run_id, wake_id, session_id, status, spoke, assistant_message_id, assistant_reply, tools, finished_at`。status 仅 done/cancelled/error，finished_at 固定、带时区且不早于 occurred_at。

- spoke=true：仅 done；assistant_message_id 是已提交的真实 assistant seq，assistant_reply 是非空实际正文。必须与原文提供方的精确路径、角色和正文一致，不能引用 start 窗口已有消息号。
- spoke=false：assistant_message_id 必须显式 null、assistant_reply 必须空串；完全安静或只有工具行动也可 done，取消/失败不造可见回复。
- tools：最多 1000 个 `{tool_call_id,name,status,result_ref}`。status 为 completed/failed/unknown，tool_call_id 唯一。result_ref 固定 `wake-tool:<run_id>:<tool_call_id>`。

工具引用仅指 App 的本地账，返回 `tool_evidence=app_metadata_only`；不访问该指针，不把它当已验证正文或长期 Event 证据，也不覆盖 Ember 自己的 MCP 工具回执。

先持久固定 finish 意图、冻结新工具调用（state=finishing），再校验可见回复。临时原文不可用返回 503 run_source_unavailable + Retry-After:2，保留该意图，App 继续投相同 event_id、正文、工具和时间。进程重启后也只能续验该意图；不能用另一终态或另一 body 替换它。真正路径/角色/正文不匹配返回 409 run_source_mismatch，留待对账，不谎报 done。

成功返回 200、同一 run/wake/session、原终态、event_id、spoke、assistant_message_id、finished_at 和工具元数据。首个终态不可变；相同操作重放原结果，不重读原文或重执行工具，新操作指向终态返回 409 run_finished。存储 finish 请求哈希和无正文元数据，不落 assistant_reply。

## GET /ember/runs/{run_id}

返回 200，身份与 `status=running/finishing/done/cancelled/error`、开始版本/路径、`execution_context_available`、`finish_event_id`、`tool_calls`（调用身份、名称、执行状态和结果状态/code，无正文）。终态附 spoke、真实回复 seq/工具元数据与 finished_at；运行中的 spoke 未知，不填 false 冒充安静结果。

只有确实无该实例记录才 404 `run_not_found`。finishing 是待固定完成意图核验，不是失败/取消。重启保留 running/finishing/终态，不自动改成 error，也不恢复执行权：GET 的 execution_context_available=false 时不得重跑模型/工具；App 用其本地持久结果补投 finish，未知则保留对账槽。

## MCP 请求绑定及首版权限

每次工具调用带 `X-Ember-Run-Id` 和稳定 `X-Ember-Tool-Call-Id`。不可混用 `X-Ember-Recall-Id`，缺/空/未知/已结束或上下文丢失均拒绝，不能回退到普通对话。工具清单沿用实例通用清单，App 必须与 start 返回 allowed_tools 取交集；服务器也会拒绝其余工具。

首版只准 `read_memory_source`。**search_memory 不是纯读**：现有实现会记提取账、刷新遗忘时钟和恢复 Cue，所以首版返回 `run_tool_not_allowed`。App 当前 WAKE_READ_TOOLS 含该项，需要移除或按 allowed_tools 过滤；不要求改普通对话搜索语义。

调用身份先持久登记再执行；同 ID 改内容 409，未知结果不重执行。读结果仅暂存在进程内，最多保留 256 条；缓存淘汰/重启后同身份返回 result_not_retained，不悄悄重读。未完成结果返回 tool_outcome_unknown。已存在的完成回执可重放，终态之后的新调用拒绝。实际读出的正文、异常原文和工具结果不写入 SQLite。

## HTTP 拒错摘要

| HTTP | code / 含义 |
|---|---|
| 400 | invalid_arguments / invalid_identity，含具体 field（如有） |
| 401 | Bearer 认证失败 |
| 404 | 实例未知；或 run_not_found（确无记录） |
| 409 | id_conflict / context_mismatch / run_busy / run_finished / run_source_mismatch |
| 503 | run_source_unavailable，继续投同一固定 finish |

MCP 失败仍为正常 RPC 响应中的 `isError=true` 与业务 code；run_context_lost、run_tool_not_allowed、tool_outcome_unknown、result_not_retained 必须保留各自语义。

## 明确未开放

主动 create/revise/pin/unpin、专用工具结果长期证据、主动 Event 原文协议和 pending 写入均未开放。无显式调用不建待写记忆；本版 finish 不创建 Event，不向 Heart 发 echo，不训练耳朵、不改变调度或日常开关。完整真实三方联调、断线/重启组合以及日常部署需在 App 接收上述字段/工具范围后进行。

## 验收结果

最终实现的 VPS 临时目录完整回归 293/293 通过，旧 sidecar 38/38。新增主动 run 专项 24/24（包含 4 项 HTTP/MCP 和 2 个真实进程崩溃检查），资源警告按错误模式也通过；可见回复按提供方的实际返回顺序核对，不用编号集合代替顺序。App 既有真实进程隔离 wake-http-check 20/20；导出的 3 个真实 start 请求直接通过 Ember 校验。它们不等于真实 App + Heart + Ember 三方已联调，更不代表日常主动发送已开启。
