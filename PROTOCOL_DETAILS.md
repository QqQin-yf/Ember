# 协议细节

HTTP 入口和字段见 [API.md](API.md)，记忆生命周期见 [MEMORY_LIFECYCLE_SPEC.md](MEMORY_LIFECYCLE_SPEC.md)。所有身份和回执按实例隔离，同一实例目录由一个服务进程持有。

<a id="briefing"></a>

## Briefing

POST /ember/briefing/refresh 接收 session_id、keep_from_message_id，可附 context_message_ids。刷新先同步原文增量、等待关系压缩和抽点，再保存完整文本、revision、refreshed_at 和统计。拼装本身不调用模型，前置处理可能调用模型。

内容顺序为关系时间线、其他 pinned Event 原文、近期非关系且非 promise 事件、pinned 认知/规律、到期 open 约定及待处理数量。近期层使用剩余预算，从低分和较旧条目开始移除；到期约定和待处理提示不砍。现场来源完整保留的 Event 不进入 briefing。

GET /ember/briefing 只读保存的快照；无快照返回 empty，损坏返回 briefing_snapshot_unreadable。对话中不动态替换快照。接入方在截断、新会话或重新进入需要新简报时刷新，将完整文本放入上下文。

<a id="generation-binding"></a>

## 普通生成与工具绑定

turn_start 提供稳定 recall_id、真实 session_id、用户正文和 user_message_id。实例与 recall_id 确定生成身份。发生时间和活动/窗口版本由接入方提供，重试保持原值。

| 请求字段 | 规则 |
|---|---|
| user_message_id | 原文服务全局 seq，正整数或十进制字符串，不是消息 UUID |
| context_start_id | 当前上下文起点 seq |
| context_message_ids | 当前分支实际保留的精确消息 ID，可表达分离区间 |
| occurred_at | 可选带时区时间字符串 |
| activity_revision / window_revision | 可选非负整数 |

可选时间和版本缺失时保存 null，不按查询时状态补齐。请求业务指纹包含会话、正文、用户 seq、heart_turn_id、上下文边界和提供的时间/版本；delivery 不进入业务指纹。相同 ID 改动业务内容返回 id_conflict。未绑定 recall_id 的调用不具备召回快照重放能力。

### MCP 请求

普通生成的每次工具调用携带 X-Ember-Recall-Id 和 X-Ember-Tool-Call-Id。调用先登记身份再执行，同一调用 ID 的名称和参数必须一致。新调用要求仍可执行的生成上下文，已完成的回执可重放；读取回执不恢复生成权限。

| 情况 | 行为 |
|---|---|
| 身份不完整或混用 run_id | 拒绝，不回退到其他活动生成 |
| 同工具调用 ID 改名称或参数 | id_conflict |
| 已完成的持久回执 | 返回原结果，不重执行 |
| 已接受但无已提交结果 | tool_outcome_unknown，不重执行 |
| 原文易失结果不在进程缓存 | result_not_retained，不按原调用身份重新读取 |

GET /ember/generations/{recall_id} 查询生成状态。GET /ember/generations/{recall_id}/tools/{tool_call_id} 返回工具名称、执行状态和可用回执；不存在的调用返回 unknown_tool_call。

<a id="recall-refs"></a>

## 召回呈现与出处引用

turn_start 的 presentations 按实际呈现顺序记录内容；recall_refs 保存触发会话、用户锚点和实际输出的 Event 出处。

`event_text` 和 `entity_overview` 呈现项带 `text`，分别与 `event_id`（及 `version_id`）或 `entity_id` 绑定。`text` 是本次 `injected_text` 中该项的完整展示文本块（含该项标题和提示），不含全局前缀、附加原文切片或其他呈现项；片段命中后展示的 Event 也遵循此约定。原有 `injected_text`、引用、顺序和调用参数保持兼容。

传入 `recall_id` 时，`text` 随首次完整响应一起冻结；同 ID 重试和 `?view=response` 返回冻结文本，不读取最新 Event／Entity 重建。正文沿用完整响应的 24 小时有效期；默认 refs 视图的 `snapshot.presentations` 不含 `text`，长期保留的引用元数据和切片诊断也不复制该正文。已冻结的旧响应保持原样，不补填新增字段。

| presentation | 内容 | Event 引用资格 |
|---|---|---|
| event_text | Event 正文 | 有 |
| event_excerpt | Event 摘要或摘录 | 有 |
| entity_overview | Entity summary 或概览 | 不展开隐藏成员 |
| retrieval_hint | 检索指引 | 无 |
| theme | 主题概括 | 无 |
| knowledge | 认知/规律 | 无 |
| source_slice | 原文切片 | 通过切片来源下钻，不扩充隐藏 Event |

轻量条目可带 light=true。recall_refs.events 只收实际展示内容的 Event，按事件去重保序，同一事件正文优先于摘录。候选、落选条目和 Entity 隐藏成员不进入该数组。

Event 引用包含：

| 字段 | 内容 |
|---|---|
| event_id / version_id | 呈现时的 Event 身份，缺版本时为 null |
| presentation | event_text 或 event_excerpt |
| source_session_id | 原文会话 |
| source_turn_spans | 可分离的来源轮次区间 |
| turn_messages | 每轮已知的 user_message_id、assistant_message_id，可附 source_recall_id |
| source_evidence | 来源身份的证据类型 |
| resolution / reason | 原文路径解析状态及原因 |
| source_leaf_id / message_ids | Ember 不持有消息父链，未解析时为 null |
| source_slices | 切片写入 Event 的冻结来源记录，可跨会话 |

anchor_message_id 和来源消息号使用全局 seq 字符串，缺失为 null。来源不足时保持 unresolved，不用空数组代表已解析成功。

### 冻结来源

区间来源保存在 event_sources.jsonl，记录初始 Event 身份、版本、会话、区间、逐轮消息身份和可信标记，不保存聊天正文。来源记录先于 Event 写入，中断可能留下孤立记录。

来源编号来自同一份定位语料。最大回复号、数值范围、当前叶节点和 Event 创建轮次不能代替逐轮证据。首次来源固定，收轮补交以独立证据保存，不覆盖原记录。含未知或冲突身份的语料不作为可解析路径的端点证据。

| source_evidence | 含义 |
|---|---|
| frozen_messages | 写入时冻结的已知消息身份，可能仍缺部分编号 |
| generation_completed | 同生成 done 回执证明缺失来源轮的完成回复 |
| legacy_unverified | 没有可核验的冻结来源 |
| unverified_corpus | 定位语料身份不可信或冲突 |

缺会话返回 source_session_unknown，缺区间返回 source_span_missing，可信出处缺消息号返回 source_seq_missing，缺路径证明返回 source_path_unknown。接入方按真实消息库核验父链，Ember 不自行将这些引用标为 resolved。

<a id="recall-snapshots"></a>

## 召回快照

### 开轮与重放

成功的 turn_start 响应、呈现和引用元数据冻结保存，包括空召回和只有 Entity 概览的结果。保留期内相同 recall_id、相同业务内容重放原响应，不重新选材、不增加 turn、不重新计数。真正重新生成使用新的 recall_id。

查询或重放不恢复活动生成上下文，也不授予工具写入权限。Event 内容或 pin 状态变化不改写已冻结的响应。

### 查询

GET /ember/recalls/{recall_id} 默认 view=refs；?view=response 返回首次完整响应。沿用 Bearer 和 X-Heart-Instance。

| 字段 | 内容 |
|---|---|
| recall_id / session_id / turn | 原生成身份 |
| status | ready，或相应的处理中/失败状态 |
| snapshot | 冻结的 request_context、presentations、recall_refs；refs 视图不含注入正文 |
| source_candidates | 可独立修订的来源候选，初始取自冻结 Event 引用 |
| sources_revision | 来源候选修订号，初始为 0 |
| response_available / expires_at | 首次完整响应是否仍可读取及到期时间 |
| response | 仅 response 视图提供，值为原 turn_start 响应 |

| 情况 | HTTP / code |
|---|---|
| 已完成 | 200 / status=ready |
| 已接受，尚未提交结果 | 202 / recall_in_progress |
| 结果已固定，记账待恢复 | 202 / recall_recovery_pending |
| 接受后中断且没有已提交结果 | 409 / recall_outcome_unknown |
| 生成登记缺少快照 | 409 / recall_snapshot_unavailable |
| 正文过期，重复 start 或读取 response | 410 / response_expired |
| 实例中不存在该身份 | 404 / unknown_recall |
| 不支持的 view | 400 / invalid_arguments |
| 同 ID 不同业务内容 | 409 / id_conflict |

202 响应带 Retry-After: 2，表示仍需查询，不能作为成功空召回。新 start 因既有记账待恢复而尚未接受时返回 409 ledger_recovery_pending；恢复后可重试该原请求。

### 正文与持久化

完整响应从首次提交起保留 86400 秒，读取和重试不续期。到期清理 response_json，身份、引用元数据、来源修订、记忆和工具回执保留。refs 仍可查询，原 recall_id 不可复用。

sidecar 启动、查询、实例加载及每分钟任务执行清理。停机期间未执行的清理在恢复运行后补做。SQLite 使用 secure_delete；外部备份不属于这项清理范围。

SQLite 原子占位生成身份和 turn，再计算召回。同一身份只有一个创建者；预留 turn 中断后不复用。最终响应、引用元数据和待应用记账操作在同一事务提交，JSONL 记账用稳定 operation_id 防重并可恢复。

提交结果前中断保留 unknown，不重新选材；提交后记账中断则恢复原操作和原快照。Event JSONL 与工具 SQLite 回执不是同一事务，结果未知的工具不能盲目重执行。

<a id="runs"></a>

## 主动 run

协议版本为 ember-runs/1。每实例使用 runs.sqlite3，身份与普通生成分离。请求沿用 Bearer 和实例头，不创建普通 turn 或自动召回。

POST 使用严格 JSON 对象，所有必填键均须提供，可空字段也须显式传 null。标识符为无首尾空白的非空字符串，最多 512 字符。消息号为正整数或十进制字符串，不接受布尔值或重复编号。

### POST /ember/runs/start

| 必填字段 | 规则 |
|---|---|
| event_id | 稳定 start 操作身份 |
| kind | wake |
| run_id / wake_id / session_id | 运行、候选和真实会话身份 |
| activity_revision / window_revision | 非负安全整数，重试固定 |
| leaf_id / context_start_id | 不透明消息 UUID 或 null，不使用 seq 代替 |
| messages | 最多 10000 个 message_id / role 对象，role 为 user 或 assistant，无正文 |
| occurred_at | 带时区 ISO 时间，重试固定 |

返回 status=running、kind=wake、origin=assistant_initiated、protocol_version、运行身份、开始版本和路径信息，以及 allowed_tools=[read_memory_source]、memory_writes=false、window_evidence=app_attested。

同一 event_id 同内容重放原响应，不恢复执行权。同 run_id / wake_id 换操作身份或内容冲突返回 id_conflict。每实例最多一个 running / finishing run，已有运行占用时返回 run_busy。

### POST /ember/runs/finish

必填 event_id、kind、run_id、wake_id、session_id、status、spoke、assistant_message_id、assistant_reply、tools、finished_at。status 为 done / cancelled / error，finished_at 带时区且不早于 occurred_at。

- spoke=true：status=done，提供已提交的真实 assistant seq 和非空实际正文；不能使用 start 窗口已有消息号。
- spoke=false：assistant_message_id=null，assistant_reply 为空串。
- tools：最多 1000 个 tool_call_id / name / status / result_ref 对象。调用 ID 唯一，status 为 completed / failed / unknown；result_ref 为 wake-tool:<run_id>:<tool_call_id>。

工具引用仅作接入方元数据，返回 tool_evidence=app_metadata_only，不作为已验证正文或记忆证据。

服务先固定 finish 意图并进入 finishing，冻结新工具调用，再向精确路径提供方核对可见回复。临时不可用返回 503 run_source_unavailable、Retry-After: 2；重试须保持同一意图，重启也只能续验该意图。路径、角色或正文不匹配返回 409 run_source_mismatch。

成功返回终态、运行身份、spoke、回复 seq、工具元数据和 finished_at。终态不可变，相同操作重放原结果，不重新读取原文或执行工具；新操作指向已结束 run 返回 run_finished。持久记录不包含 assistant_reply 正文。

### 查询与 MCP

GET /ember/runs/{run_id} 返回 status、开始身份和窗口、execution_context_available、finish_event_id、tool_calls 以及终态元数据。不存在时返回 run_not_found。

重启保留 running / finishing / 终态，不恢复进程执行上下文。execution_context_available=false 时不能重跑模型或工具；可用接入方持久结果补交原 finish。

工具请求携带 X-Ember-Run-Id 和 X-Ember-Tool-Call-Id，不可混入 X-Ember-Recall-Id。只允许 read_memory_source；其他工具返回 run_tool_not_allowed。调用结果未知时不重执行，易失正文缓存最多 256 条，淘汰或重启后返回 result_not_retained。终态后的新调用拒绝，已有完成回执可重放。

| HTTP | code |
|---|---|
| 400 | invalid_arguments / invalid_identity |
| 401 | unauthorized |
| 404 | 实例未知 / run_not_found |
| 409 | id_conflict / context_mismatch / run_busy / run_finished / run_source_mismatch |
| 503 | run_source_unavailable |

MCP 工具失败通过正常 RPC 结果中的 isError=true 和业务 code 返回。run_context_lost、tool_outcome_unknown、result_not_retained 分别表示执行上下文丢失、结果未知和正文未保留。

<a id="source-completion"></a>

## 来源补交

写入时来源轮的 assistant seq 可能尚未产生。同生成 done 证明该轮实际提交的回复后，系统将其作为独立完成证据补交，不覆盖原冻结记录、Event 内容、来源跨度或首次召回响应，也不替尚不存在的引用补证。

### 补交条件

1. Event 具有已登记生成的 writer_recall_id、writer_tool_call_id、完成且返回该 Event ID 的工具回执及冻结来源。
2. 待补轮属于同一实例、会话和原来源跨度，用户 seq 与原生成真实锚点一致。
3. pending_sources 明确绑定缺失位置的 turn、user_message_id 和 pending_source_recall_id；写入时不存在该轮已知 assistant 语料。
4. 收到同生成的 done，具有真实最终回复和 assistant seq，并通过终态身份与业务哈希校验。
5. 已知 seq 不被覆盖。error / cancelled、结果未知或缺身份的来源不补。

多轮 Event 只补与该生成确切绑定的来源轮。没有逐项证据的记录继续 unresolved。

### 候选修订

补交绑定原出处哈希及来源轮。source_completions 与 recall_sources 在 generations.sqlite3 中保存独立证据、候选和修订，不复制回复全文。

仅对原冻结列表已包含该 Event 且身份匹配的召回更新 source_candidates，不新增隐藏成员。候选实际改变才将该 recall 的 sources_revision 加一；重复 done、查询或恢复不重复推进。新创建的召回可冻结当时已有的完成证据，初始修订仍为 0。

补交和受影响候选在同一事务发布。启动、查询、开轮和重复收轮可恢复未完成更新。done 已提交而补交暂时失败时，done 回执有效；refs 查询在恢复失败期间返回 recall_recovery_pending。正文 TTL 不因来源修订延长。

### 完成证据

```json
{
  "source_evidence": "generation_completed",
  "turn_messages": [{
    "turn": 7,
    "user_message_id": "101",
    "assistant_message_id": "102",
    "source_recall_id": "r-original"
  }],
  "completion_bindings": [{
    "turn": 7,
    "recall_id": "r-original",
    "user_message_id": "101",
    "assistant_message_id": "102",
    "evidence": "turn_end",
    "status": "done"
  }],
  "resolution": "unresolved",
  "reason": "source_path_unknown"
}
```

接入方核验补交与来源轮的身份、编号及真实父链后，才能解析路径。generation_completed 本身不等于 resolved；混合来源中的其他未补位置仍需各自证据。

<a id="turn-end-recovery"></a>

## 轮后完成与恢复

### 补发条件

接入方同时持有原生成的可靠登记和真实终态时，可补发 turn_end。可靠登记来自成功 start 或 refs 查询的 ready 结果，须核对实例、session_id、recall_id、用户锚点和正整数 turn。done 还需原生成已提交的非空最终回复和真实 assistant seq。

终态先到时等待登记恢复；登记先恢复时等待真实终态。空召回和仅有 Entity 概览的生成同样需要收轮。补发不重跑模型或工具，也不重新调用 turn_start。

### 请求

POST /ember/hooks/turn_end 使用原 Bearer、实例头及生成身份：

```json
{
  "session_id": "original-session",
  "recall_id": "r-original",
  "turn": 7,
  "status": "done",
  "assistant_message_id": "102",
  "assistant_reply": "该生成实际提交的最终回复"
}
```

turn 使用 Ember 原响应或查询给出的轮号。回复 seq 属于原生成，不能换成兄弟回复、当前叶节点或未提交编号。error / cancelled 使用真实终态，可省略回复正文和 seq。收轮不使用工具调用 ID 或额外 event_id。

### 幂等与重试

终态请求的会话、轮次、status、正文和 seq 固定后原样重试。相同内容返回原回执，不增加 turn；不同内容返回 id_conflict。仅状态和 seq 相同不足以证明正文相同。

| 结果 | 处理 |
|---|---|
| 200 且 ok=true | 核对归属后完成 |
| 超时、网络错误、5xx | 保留原请求，退避重试 |
| id_conflict / context_mismatch | 保留冲突，核对原登记，不覆盖或换 ID |
| generation_not_ready | 查询原登记，不伪造 turn |
| unknown_recall | 核对实例和原账，不补发 start 或移除 recall_id |
| response_expired | 改查 refs；可靠登记和真实终态齐备时仍可收轮 |
| recall_in_progress / recall_recovery_pending | 无可靠成功登记时等待查询 |
| recall_outcome_unknown / recall_snapshot_unavailable | 无可靠成功登记和原 turn 时不自动补发 |

已被其他生成替代或重启丢失上下文的收轮，只补原终态和映射，不将回复插入当前生成，也不关闭当前生成。查询和正文 TTL 不取消独立收轮任务。

JSONL 映射与 SQLite 终态不是一个事务；终态结果按身份和内容幂等，不能据此重执行结果未知的工具。
