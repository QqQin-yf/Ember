# 召回快照：查询、重试与 24 小时正文留存

2026-10-21 更新：同生成来源补交及 sources_revision 推进已实现，见 SOURCE_COMPLETION_SPEC.md。原 snapshot/response 冻结；source_candidates 独立持久更新，实际变化时修订 +1，新召回初始为 0。下文首版固定 0 的描述仅对应历史 156f29e；运行版本见隔离就绪回执。

2026-10-20。本地已实现，未提交、推送、部署或做真实三方联调。用户已确认临时召回正文保留 **24 小时**。本文取代此前文档中“快照未实现、TTL 待确认”的阶段说明；主动 run 仍未实现。

## 1. 开轮和重试

沿用 `POST /ember/hooks/turn_start`，提供真实 `session_id`、`message`、`user_message_id` 和稳定 `recall_id`。普通工具头和收轮要求见 [GENERATION_BINDING_SPEC.md](GENERATION_BINDING_SPEC.md)。不带 recall_id 的旧模式不获得快照幂等能力。

新增可选请求字段：

| 字段 | 格式 | 规则 |
|---|---|---|
| occurred_at | 带时区时间字符串 | app 保存的原始发生时间，重试原样携带 |
| activity_revision | 非负整数 | 本次生成开始时的活动版本 |
| window_revision | 非负整数 | 本次生成开始时的窗口版本 |

这三个字段缺失保存为 null，不推算版本，也不以查询时的当前值补齐。user_message_id 是真实全局 seq（数字或数字字符串），不是消息 UUID。context_start_id 仍按原约定转换为 seq。

身份键为 `(instance, recall_id)`。业务指纹固定会话、用户正文、真实用户 seq、heart_turn_id、context_start_id 以及提供的原始时间/版本；同 ID 改动这些内容返回 409 id_conflict。delivery 是投递信息，不进入业务指纹。相同时间的不同字符串表示也不自动归一化，app 应重发原值。

第一次成功仍返回原 turn_start 响应，包括 injected、injected_text、session_id、turn、memory、recall_id、presentations、recall_refs。同 ID 同内容在保留期内返回同一份 JSON 数据，不重新召回、渲染、增加 turn、计数或刷新提取时间。事件之后被修改或 pin 状态改变不改变历史响应。成功的空召回和只有实体概览的召回同样冻结。

查询或重试均不恢复旧活动槽或工具写入权限，也不把历史结果重新注入当前模型。app 应另行检查本次生成和活动版本；真正重新生成使用新 recall_id。

## 2. 查询接口

`GET /ember/recalls/{recall_id}`，沿用 Bearer 鉴权和 `X-Heart-Instance` 实例路由。默认 `view=refs`；仅需要恢复完整原始响应时使用 `?view=response`。该接口与 Heart 的召回登记接口属于不同服务。

ready 的 refs 示例：

```json
{
  "recall_id": "r-102",
  "session_id": "s1",
  "turn": 7,
  "status": "ready",
  "snapshot": {
    "session_id": "s1",
    "turn": 7,
    "request_context": {
      "session_id": "s1",
      "heart_turn_id": null,
      "user_message_id": "101",
      "context_start_id": null,
      "occurred_at": "2026-10-20T08:00:00Z",
      "activity_revision": 4,
      "window_revision": 2
    },
    "presentations": [],
    "recall_refs": {
      "recall_id": "r-102",
      "session_id": "s1",
      "anchor_message_id": "101",
      "events": []
    }
  },
  "sources_revision": 0,
  "source_candidates": [],
  "response_available": true,
  "expires_at": "2026-10-21T08:00:01Z"
}
```

示例数组仅示意字段。实际 presentations 和 recall_refs 按首次实际呈现保存；实体概览不会展开成事件成员，未知来源维持 unresolved。refs 视图没有注入正文或用户正文。response 视图在上述外壳中另加 `response`，其值为首次完整 turn_start 响应；不是把响应字段直接铺到查询顶层。

source_candidates 初始等于冻结 recall_refs.events 的独立副本，sources_revision 初始为 0。已实现可信同生成 done 完成绑定：只补原成员的确切缺失位置，独立候选实际变化才持久推进修订，原快照不变；重复查询/收轮不推进。使用 generation_completed 证据，不以最大 reply、当前 Event 或实体成员表重建历史。App 需接纳并核验新证据再验证真实路径；长期无法证明的事件仍 unresolved，详见 SOURCE_COMPLETION_SPEC.md。

| 情况 | HTTP / code | app 处理 |
|---|---|---|
| 已完成 | 200 / status=ready | 读取相应视图 |
| 已接受，尚未提交结果 | 202 / recall_in_progress | status=in_progress，Retry-After: 2；同 ID 查询、有限退避 |
| 结果已固定，记账待恢复 | 202 / recall_recovery_pending | 同 ID 查询，继续恢复原操作 |
| 接受后中断且没有已提交结果 | 409 / recall_outcome_unknown | 不用同 ID 重跑，不伪报空召回 |
| 旧版生成登记没有快照 | 409 / recall_snapshot_unavailable | 不能重建或重跑旧 ID |
| 正文过期，读取 response 或重复 start | 410 / response_expired | refs 仍可查询，旧 ID 不复用 |
| 该实例从未登记 | 404 / unknown_recall | 与空召回、已过期区分 |
| 不支持的 view | 400 / invalid_arguments | 修正查询 |
| 同 ID 异业务内容（start） | 409 / id_conflict | 核对原请求；真正新生成使用新 ID |

**202 属于 HTTP 成功范围，但不是召回完成。** app 不能仅用 response.ok 判定可用，也不能把缺少 refs/turn 的 pending 响应登记成 no_refs 或 events=[]。普通聊天可降级继续；恢复结果只能对账，不重跑模型或工具。

新 start 如因更早召回记账待恢复而尚未接受，返回 409 ledger_recovery_pending；此时新 ID 尚未登记。恢复后可重试原请求，不把这一状态当成已接受的新召回。

## 3. 正文保留规则

- 从完整响应首次持久提交开始计时，固定 86,400 秒，不从用户消息时间计算，读取和重试不续期。
- 到期清空本实例 generations.sqlite3 的 response_json，包括临时 injected_text/统计响应副本；原有 Event、app 聊天、工具回执不随这项 TTL 删除。
- 会话/轮身份、业务哈希、呈现分类和引用元数据继续保留，用于防止旧 ID 复用及迟到解析。不是删除整份记忆，也没有复制 app 全部原文。元数据的后续清理与会话删除通知尚无接口。
- 查询严格检查到期；运行中的 sidecar 启动即清理，之后每 60 秒检查已配置实例，无请求时也清理。实例服务加载时亦清理。服务停机期间无法执行清理，恢复运行后补清。
- SQLite 清理连接启用 secure_delete，避免仅将字段置空却留下可直接从数据库空闲页读出的旧副本；不承诺清除外部备份、操作系统缓存或磁盘历史。
- read_memory_source 的工具读取回执仍只在进程内保存原文，不因本规则改为持久保存。24 小时规则只用于轮前召回响应快照。

## 4. 持久化与恢复边界

同实例由一个服务进程拥有。先在 SQLite 原子占位身份和预留 turn，再计算召回；同 ID 只有一个创建者，其余返回已固定结果或 pending。预留 turn 在中断后也不复用。

最终响应、引用元数据、生成状态和待应用记账操作在同一 SQLite 事务提交。之后将带稳定 operation_id 的轮前映射和召回统计写入 JSONL，flush/fsync 后确认；启动/查询/下一次开轮可重放未确认操作。JSONL 索引按 operation_id 防重，保留第一次的时间，修复截断尾行后继续追加。不把旧 hook 整体先跑完再写缓存。

未提交结果前中断，重启后保留 unknown 身份而不重新选材；提交后记账中断，恢复原操作并返回原快照。恢复不重新打开旧生成。记忆工具的 Event JSONL/工具回执仍遵守原有不确定结果纪律，不能据快照事务宣称所有工具跨存储 exactly-once。

## 5. app / Heart 下一步

2026-10-21 更新：App d62aad1 已报告本地接入 turnEnd.recall_id、逐请求普通 MCP 两个身份头及恢复分支，假服务验证通过，尚未真实联调。迟到 ready 的原轮次 turn_end 需要补发，持久时机、状态和幂等规则见 [TURN_END_RECOVERY_SPEC.md](TURN_END_RECOVERY_SPEC.md)；正文到期但 refs ready 时仍可补。由 app 唯一驱动迟到解析；原始 Heart revision=0 登记持久确认后，解析变化才另发 resolution 操作，原始包不改。Ember 不直推 Heart、不定时替 app 补解析。

Heart 可查看本文及 [APP_交接回复.md](APP_交接回复.md)；快照补取不新增用户刺激、不复活旧唤醒周期。原文读取 `/api/source/path` 客户端迁移、独立主动 run、工具结果证据与显式 pending 核验仍未完成。临时提醒不入档、不自动召回；整个唤醒链路不训练耳朵，不新增每次唤醒自动写记忆。

验证：191 项 unittest 通过，含快照重试、实例/鉴权隔离、真实 ASGI pending、精确 24 小时到期、数据库正文清除、并发单创建者、提交前后故障与 JSONL 恢复。真实三方联调尚未执行。
