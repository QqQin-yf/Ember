# Ember 对 app 离线交接的回复

## 来源完成绑定交付：可补验真实解析（2026-10-21）

SOURCE_COMPLETION_SPEC.md 已按用户确认实现。新 Event 出处保存明确 pending_sources；同生成 done 后核对原开轮、完成工具回执、用户 anchor 和来源轮，独立持久保存 generation_completed / completion_bindings。原 event_sources 首条与原召回 snapshot/response 均不改；只更新确已含该事件的 source_candidates，实际变化才 sources_revision +1。新召回若已有完整绑定，初始 revision=0。

现有 156f29e 的 schema_version=1 记录仅在全部持久证据吻合时兼容补交；legacy 无身份、错误锚点、未知工具结果、已有确切 assistant 或不可信语料不回填。done 不是对未来回复参与旧 quote 核验的追认。细节、字段与故障恢复以 SOURCE_COMPLETION_SPEC.md 为准。

验证：213 项 unittest 通过。隔离地址仍 http://127.0.0.1:8878，实例 default/phase2，凭据仍 D:/phase2-secrets/app.env；加载提交、PID 和实际探针结果见 D:/phase2-secrets/ember-readiness.json。本段优先于下方“sources_revision 固定 0 / 尚未实现”的历史记录。

App 下一步接纳并核验 generation_completed 和 completion_bindings，查询到来源修订后用 source_candidates 走真实父链，再验证 Heart 独立 resolution。原始包不改、额外证据字段不透传 Heart。App 重启旧队列 backfill 按 Heart 已确认规则单独处理，Ember 不新增协议；原文提供方限制和旧 source 迁移仍单列。此回执不宣称 App resolved/resolution 已验通。

## 对 App §9 / ecd29c6 的回执：同生成出处补交（2026-10-21）

已读 §9，知悉 recall_id 已增加持久安装号以避免跨数据库撞号；Ember 将它作为不透明稳定身份，不解析其数字或前缀。普通链路、工具身份、迟到收轮与 App 重启补投按 App 的真实三方对账报告记录；本次未代为重跑，resolved/后续迟到解析仍未验通，原文提供方未配置仍是独立限制。

接受同生成 done 后由 Ember 负责独立补交来源绑定，但不能直接覆盖原冻结出处或拿 writer_recall_id 填满历史来源。只补已证明属于该生成的原来源轮；补的是闭合路径，不追认最终回复在写入时已经参与引用核验。完整方案和归属见 [SOURCE_COMPLETION_SPEC.md](SOURCE_COMPLETION_SPEC.md)。

对已包含该事件的旧 recall：原 snapshot/response 不改，独立 source_candidates 实际变化时 sources_revision 持久 +1；同 done 重试/查询/重启不重复增加。补交先于新召回时，新召回可直接冻结已有证据，初始修订仍为 0。写记忆那轮的开轮快照若不含新事件，不给它添成员。

App 需配合：新证据类型 generation_completed 及 completion_bindings 的核验；当前仅接受 frozen_messages，不能只等修订号增加而不接新证据。随后仍由 App 唯一做真实父链解析，初始登记确认后才发独立 Heart resolution；新核验字段不透传 Heart。

本次仅写方案，未改业务代码、未提交新实现、未重启隔离实例；8878 仍是 156f29e、sources_revision 仍为 0。下一段按配套方案实现并验收，不把本回执当成已补齐真实出处。凭据现已另交付到用户指定的 D:/phase2-secrets/app.env，勿输出其内容。

## 对 App b929d44 / §7—8 的交付安排（2026-10-21）

已读 §7—8，迟到收轮与完成活动水位接线已由 App 报告完成；其真实 Heart 验证与 stub 验证范围按原回执保留，真实 Ember 链路待联调。Ember 提交本仓库呈现/出处、普通生成绑定、快照及补收轮交付，并提供真实本机隔离 sidecar，部署说明见 PHASE2_INTEGRATION.md；具体提交/PID/路径以私有运行目录 runtime.json 和本次交付消息为准。此段优先于下方历史“尚未提交”状态。

Ember 地址为 http://127.0.0.1:8878，实例 default/phase2；独立 token 由当前 Windows 用户专属目录中的 token.txt/app.env 交付，不贴聊天、不入 git。原文提供方未配置，不能据此宣称旧 source 已迁移。生产及 App 开关不变，真实三方联调仍需 App 按 §8.4 执行。

## 对 App d62aad1 / §6 的回复：需要补收轮（2026-10-21）

已读 APP_第二阶段回复.md §6，核对提交 d62aad1 及查询/收轮客户端。接受本轮普通头、收轮身份与快照恢复已由 App 本地接入的回执；验证仍仅为假服务，不视为真实联调，两个开关保持关闭。下方“App 尚未补身份头/收轮身份”仅为此前时点记录。

**§6.3 第 2 项明确答复：需要补发原轮次 turn_end。** 原召回查到 refs ready 与 App 真实终态持久提交，两者都具备时，按原 instance/session_id/recall_id/Ember turn 补发；done 用原生成真实非空最终回复和 seq，error/cancelled 不带回复和 seq。建议原终态提交时就持久记收轮意图，原 turn 未知则等待登记恢复；独立重试，不依赖 Heart 召回投递或解析是否成功。

同身份同内容重发返回原终态回执，异内容 id_conflict；正文 24 小时过期不阻止 refs ready 后补收轮。仍 pending/unknown、无原成功开轮证据的 unavailable 或身份不明时不盲发、不假装取消、不重开旧 ID。具体时机、身份、状态矩阵、幂等与归档边界以 [TURN_END_RECOVERY_SPEC.md](TURN_END_RECOVERY_SPEC.md) 为准。此前“补取不补发 turn_end”的写法应被此回执取代；不重新注入模型的约束保留。

本次不修改 App 或 Ember 业务代码，不代 Heart 确认 completed_activity_revision；run、原文客户端迁移及生命周期事项仍按原分工推进。

复验：现有生成/快照 55 项通过；临时库组合实测正文过期、重启、已有新生成时补旧收轮、同内容重试与异内容冲突通过。§6.1 的 failed 不应一律解释成“Ember 没收下”：id_conflict 恰可能说明原 ID 已被接受，须保留原账对账，不能换 ID 规避。未做真实三方联调或部署。

## 最新接线状态（2026-10-21）

已读 Heart 最新集中回执：接受本次快照约定，没有新增 Heart 接口阻塞项，无需另开 Ember→Heart 推送。用户转达 app 已开始本地接线，后续等待其汇总实际结果；下方 app 代码缺口是 2026-10-20 的只读核对记录，不代表本次再次核验了 app 当前代码。Ember 本次仅同步文档，未改业务代码、重跑测试或部署。

Heart 强调的接线约束：

1. 从 snapshot.recall_refs 取引用，经 app 真实原文树核验并转换为 Heart 允许的 source 字段。不要直接透传查询外壳或 Ember events；turn_messages、source_evidence 仅用于 app 核验，Heart 严格拒绝额外字段。纯概览不扩充 events。
2. Ember sources_revision、Heart resolution_revision 与窗口/活动版本独立。sources_revision=0 不阻止 app 使用自身修复后的可靠证据解析；查询成功但解析未变化不增加 resolution_revision。初始登记持久确认后，变化才走新操作的独立 resolution，原始 outbox 正文冻结。
3. request_context 的原始时间/版本可能为 null，只能从 app 持久原请求账补回真实原值，不能用当前窗口/活动版本代替。无法构造 Heart 登记时保留待处理/不可用，不伪报空召回。
4. 按 202/409/410 的实际语义对账；补取旧结果不重新注入模型、不增加刺激、不复活旧候选、不训练耳朵。仍由 app 唯一触发迟到解析。

后续请汇总普通头/收轮身份、pending/过期恢复、迟到解析驱动的接线与本地验证结果。主动 run 与专用证据仍未交付，普通完成后的权威活动水位和元数据删除生命周期另行保留，不因快照接收而启用调度或生产。

---

2026-10-20。依据：用户转述的 app 休息前交接、`D:/deliz/APP_第二阶段回复.md` §5，以及只读核对的 recall.ts、revision.ts、ember.ts、mcp.ts；同时读取 Heart 最新联合回执。app 文档尚未写入其后完成的召回账和两个版本号，因此实现状态以本次代码核对为补充，不回写 app 仓库。

本文区分已经本地实现和下一段接口提案。后续已实现召回快照，用户已确认正文保留 24 小时；具体以 RECALL_SNAPSHOT_SPEC.md 为准。累计 191 项 unittest 通过，未提交、推送、部署或做真实三方验收，未修改 app/Heart 仓库。

## 1. 普通对话的两个请求头：确认可以用，已本地实现

- `X-Ember-Recall-Id`：本次普通生成的 recall_id。
- `X-Ember-Tool-Call-Id`：app 持久保存的 tool_use.id。
- 沿用 `X-Heart-Instance`；每次请求独立携带，身份不进入模型参数或提示词。

Ember 的普通 turn_start/turn_end 已接收 recall_id，MCP 按已登记生成绑定，不能借共享 _live_turn。结束/被替代后拒绝新的调用；已完成的同一 `(instance, recall_id, tool_call_id)` 重试返回原工具回执。结果未知不重放。具体见 GENERATION_BINDING_SPEC.md。

只读检查的 app 当前代码中，turnStart 可带 recallId，但 turnEnd 未带回 recall_id，callMemoryTool 也还没传这两个头。未来启用新模式时三处要一起接上；app 休息期间不替它修改，也不提前部署只接了一半的链路。

普通生成查询已本地存在：

- `GET /ember/generations/{recall_id}`
- `GET /ember/generations/{recall_id}/tools/{tool_call_id}`

这些是生成/工具账查询，**不是召回快照查询**，不能给 app 的 resolveLate 提供完整冻结引用。

## 2. 快照查询：已本地实现，可按此接线

接口为 `GET /ember/recalls/{recall_id}`。沿用 Ember Bearer 和实例头，不另传 body instance。本地已实现；同 ID 同内容的重复 start 在 24 小时内返回原响应，旧版无快照登记仍返回 409 recall_snapshot_unavailable。生产尚未部署，不能假定 VPS 已可调用。

同一接口默认只返回元数据（`view=refs`）；确需重取原始生成响应时才显式 `view=response`。两种读取都不触发召回、开轮、记账、刷新遗忘或恢复旧写入上下文。它与 Heart 的 `/api/wake/v1/recalls` 是不同服务的接口。

| 状态 | HTTP / code | 语义 |
|---|---|---|
| in_progress | 202，Retry-After | 该 ID 已持久接受，结果尚未固定；不伪报 events=[] |
| ready | 200 | 固定结果可读取；默认没有注入正文 |
| interrupted/unknown | 409 recall_outcome_unknown | 接受后故障，结果不确定；保留身份，不用同 ID 盲目重跑 |
| response_expired | 410 response_expired | 正文已清理，refs 仍可查；不能用旧 ID 再开召回 |
| unknown | 404 unknown_recall | 无该实例内的登记记录；不等于成功空召回 |

ready 的元数据简例（完整 request_context 和 expires_at 见 RECALL_SNAPSHOT_SPEC.md）：

```json
{
  "recall_id":"r-102",
  "session_id":"s1",
  "turn":7,
  "status":"ready",
  "snapshot":{
    "presentations":[],
    "recall_refs":{
      "recall_id":"r-102",
      "session_id":"s1",
      "anchor_message_id":"101",
      "events":[]
    }
  },
  "sources_revision":0,
  "source_candidates":[],
  "response_available":true
}
```

示例数组为空只示意字段；实际有事件时必须保存实际呈现的原始条目，不从当前 Event/实体成员表重建。生成发生时的版本、锚点和身份也须随原始请求固定；不是查询时取 app 最新版本来替换。

- `snapshot` 是不可变原始呈现/引用，`snapshot.recall_refs` 与首次响应一致，不因迟到解析改变。
- `source_candidates` 是用于迟到解析的独立出处辅助视图，仅限原始快照内同一事件/内容身份；初始可直接等于原始 events 的出处辅助。不包含新成员，不重新读取后来改写的 Event 来换内容。
- `sources_revision` 是 Ember 出处辅助的版本，不是 app/Heart 的 resolution_revision，也不是窗口版本。首版没有可信证据补交时固定为 0，不假装每次查询都有新映射。
- 后续若支持独立、已核验且绑定原快照的证据补交，才允许提升 sources_revision 并更新 source_candidates；原 snapshot 不变。具体证据补交入口不在本轮暗中新增，也不允许从旧最大 reply 或后来的 turn_end 自动拼回历史。
- `view=response` 在正文还保留时另给 `response`，内容是首次固定的完整 turn_start 响应。正文过期但元数据仍在时，refs 仍可读，response 查询返回 410 response_expired；旧生成不会重新获得工具写入资格。

已实现开轮前持久占位、同 ID 单创建者，以及响应/元数据/轮身份/待应用记账操作的 SQLite 原子提交；JSONL 按稳定 operation_id 恢复且防重。未落定结果不报告 ready，提交前故障保留 unknown 而不重新选材。GET 和同 ID start 重试使用同一份权威状态。

app 需额外处理：202 recall_in_progress / recall_recovery_pending（Retry-After: 2）仍未完成，不得因 response.ok 为真而登记成 no_refs；409 recall_outcome_unknown 不盲重跑；410 response_expired 转查 refs，不复用旧 ID。view=response 的完整原响应在外壳 response 字段；同 ID start 重试则直接返回原响应形状。

start 可带 occurred_at、activity_revision、window_revision，原值进入 request_context 和业务指纹；首次请求前保存，重试不能换成当前版本。缺失为 null，不推算。delivery 不参与业务指纹。

### 2.1 迟到解析由谁触发：app 唯一负责

Ember 不向 Heart 直推，也不另跑一个定时任务改 app 的解析账。app 的 recall.ts 已有 resolveLate(recallId, sources)，后续由 app 自己的可靠补偿任务驱动查询及调用它。

建议触发点：

1. 原始 Ember 请求超时但登记结果未明：按同 recall_id 查询状态，bounded backoff 重试。若先前没有把该召回初始登记给 Heart，恢复后走初始 revision=0 登记，不能直接发 resolution。
2. 初始召回已登记，仍有 unresolved：app 的原文服务恢复、实际来源证据到达/修复、或收到明确的出处辅助版本变化后，查询 refs 并调用 resolveLate。来源没有变化时不制造新修订。
3. app 启动恢复时按自己的持久召回账补偿未完成工作，不新建 recall_id，不重跑模型或工具。调度频率与退避由 app 实现；接口已可本地接线，启用前需联调并部署双方。

来源路径一旦能用 app 已持有的冻结 refs 解析，可直接重试自身解析函数，不必为了形式额外向 Ember 请求。没有可信新证据的旧事件可以长期 unresolved；快照查询不是自动补证据的承诺。

首次 Heart outbox 的 revision=0 正文永远冻结。初始已被 Heart 持久接受后，变化才走新的 resolution 操作身份。仅“outbox 中存在一条尚未失败的记录”不等于 Heart 已接收；resolution 必须按初始登记的持久顺序投递，超时先对账。恢复的旧结果也不能重新注入当前模型或复活旧唤醒周期。整个过程不训练耳朵。

### 2.2 正文留存：用户已确认 24 小时

app 只存元数据、不存 injected_text 的约定保留。Ember 要保证跨重启原响应重放，就需要保留原注入文本，其中可能有有限原文片段；不能一边说永不保存正文，一边承诺原响应重放。

用户已确认并已实现固定 24 小时 TTL，从首次结果提交计时，查询/重试不续期；到期删除 response_json，引用元数据和防复用身份继续保留，不删除记忆或 app 聊天。查询严格检查到期，运行中每 60 秒清理，启动补清。元数据清理及会话删除通知仍待另定，不把正文留存扩展成全量聊天缓存。read_memory_source 工具原文回执仍不持久保存。

## 3. 主动 run：路径和请求头可以固定；tools/证据仍是提案

主动上下文使用以下独立入口方向：

- `POST /ember/runs/start`
- `POST /ember/runs/finish`
- `GET /ember/runs/{run_id}`
- 工具回执查询提议 `GET /ember/runs/{run_id}/tools/{tool_call_id}`。

主动 MCP 请求头确认命名：X-Heart-Instance、X-Ember-Run-Id、X-Ember-Tool-Call-Id。主动不带普通 X-Ember-Recall-Id，混用拒绝；名字可以用于 app stub，但当前真实 Ember run 请求仍会 unsupported_phase，不能把命名确认当作实现完成。

start 仅登记 event_id、run_id、wake_id、session_id、kind=wake、origin=assistant_initiated、开始时活动/窗口版本、leaf_id 和可验证的真实上下文引用；不接临时提醒，不占普通 turn，不自动召回或写长期记忆。先取得 Heart 的有效 start 确认，再持久登记 Ember run，再提供依赖此上下文的工具。

finish 形状提案：

```json
{
  "event_id":"finish-run-1",
  "run_id":"run-1",
  "wake_id":"wake-1",
  "session_id":"s1",
  "status":"done",
  "spoke":false,
  "reply_message_id":null,
  "finished_at":"2026-10-20T08:00:00Z",
  "tools":[{
    "tool_call_id":"tool-use-1",
    "owner":"app",
    "name":"some_tool",
    "status":"completed",
    "result_ref":{"id":"opaque-persisted-result-reference"}
  }]
}
```

每个工具项明确稳定 tool_call_id、owner=app/ember、工具名、completed/failed/unknown、持久结果引用或 null。没有可查引用时不得冒充已核验的长期记忆证据；本段示意 opaque id 不等于已定稿的结果读取协议。Ember 自己的工具结果以本地持久回执为准，不靠 app finish 覆盖已知完成结果；其他系统的工具只登记实际报告，Ember 不接管执行。run 取消不回滚已经执行的动作。

finish 不要求 user/reply 成对。完全安静允许 spoke=false、reply_message_id=null、tools=[]；仍结束 run，不生成空气泡或叙事 Event。真实输出提交后，Ember finish 和 Heart end 两份 outbox 独立补投；重启先核对实际完成证据，不能一律改为取消。

AI 显式调用长期记忆工具时才建立写入意图；本轮正文尚未落库的对应调用才 pending。已有可靠历史证据不一律 pending。工具结果专用证据/读取和 run 原文能力实现并验收前，不在真实主动轮开放其依赖的写入工具；具体主动权限名单仍由 app 提案、用户决定。本回执不新增自动记忆 writer，不替用户批准工具名单。

## 4. 给 Heart 的五项窗口版本问题：保留其确认权

用户转述的五项分别涉及：追加消息时不重发窗口、换窗口同时提升活动版本、pending leaf 不在 done 消息数组、结束回响使用开始版本而完成又提升活动版本、窗口投递携带 delivery。

这些是 Heart/app 的窗口接收与活动水位语义，Ember 不替 Heart确认。本方约束只有：某次召回应绑定真实触发时的会话/生成及版本，重试/迟到解析不能换成当前新版本；原文路径不得靠 leaf 或 seq 大小猜测。尤其“开始版本的 end”和“完成后活动版本 +1”需要 Heart 说明比较/推进顺序，不能因为 app 本地已经增加就把正常完成误当成过期。窗口 delivery 也需 Heart 明确 live/backfill 的应用规则。

这五项应由用户转给 Heart，与 Ember 这里的快照/普通头/run 对接分开确认。不改变正文留存、耳朵边界、召回门槛或主动调度开关。
