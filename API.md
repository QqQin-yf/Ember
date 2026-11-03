# Ember 接口文档

Ember 通过 HTTP hooks 和 MCP 提供记忆写入、召回、来源读取、后台维护和提取。CC 和 App 使用同一套接口，由调用方持有对话循环；接口不按调用方区分。

## 1. 基础信息

- 协议：HTTP/1.1 + JSON
- 默认地址：`127.0.0.1:8767`
- 鉴权：配置 `EMBER_TOKEN` 后，所有端点要求 `Authorization: Bearer <token>` 精确匹配；未配置时放行。所有实例共用令牌。
- 实例路由：使用 `X-Heart-Instance` 头，或 `?inst=` 查询参数；默认 `default`，清单外实例返回 404。各实例使用独立数据目录，配置见 §10。

## 2. 通用约定

### 2.1 失败回执

工具失败返回 `status=failed` 及 `error`，可附 `code`、`field` 和 `hint`。引用原文不存在或来源不可核验时拒绝写入。MCP 返回的 `isError` 对应失败或拒绝状态。

### 2.2 stale 拒收

未绑定 recall_id 时，慢到的旧轮 `turn_end` 会被拒收，返回 `{"ok": false, "reason": "stale turn_end …"}`。带 recall_id 的绑定生成模式允许补记原生成的真实历史终态，但不会恢复旧槽或污染当前语料，见 [生成绑定协议](PROTOCOL_DETAILS.md#generation-binding)。turn 计数允许留洞。

### 2.3 窗口过滤的降级语义

召回窗口过滤使用调用方可选传入的 `context_start_id` 或精确 `context_message_ids`，不要求 Deliz 的消息树或窗口管理流程。未提供窗口身份时不按窗口排除；事件的出处区间未回填时保留。消息编号须对应已配置原文来源的真实编号，不能用 CC 消息 UUID 或临时编造的编号替代。

## 3. 调用顺序

1. 调用 turn_start，取得本轮召回内容。
2. App 将召回内容注入本轮 system，调用模型并执行 MCP 工具循环。
3. 调用 turn_end，提交最终回复、消息 ID 和生成终态。

write_event / write_promise 需要绑定进行中的生成。

- turn_end 的 turn 校验：与登记号不匹配则拒绝
- 新生成替换旧活动上下文；同 recall_id 同内容重试只重放快照，不开新轮

## 4. hooks

### 4.1 POST /ember/hooks/turn_start

开新一轮 + 本地召回，返回记忆注入文本。

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| session_id | string | — | 调用方提供的会话标识，允许 `cc-` 前缀（如 `cc-<原会话ID>`），原样保留；缺省使用默认会话 |
| message | string | ✓ | 本轮用户消息 |
| heart_turn_id | string | — | Heart 轮次 ID，格式为 `t-XXXXXX` |
| user_message_id | int/string | 绑定生成模式必填 | 真实全局消息 seq，不接受消息 UUID |
| context_start_id | int/string | — | 当前窗口起点 seq：来源全在窗口内的记忆不再召回 |
| context_message_ids | array[int/string] | — | 当前分支实际保留的精确消息 ID，可表达分离区间 |
| recall_id | string | — | 稳定普通生成身份；启用时 session_id 也必填，重试原样携带 |
| occurred_at | string | — | 原始发生时间，需带时区；绑定生成模式固定保存 |
| activity_revision | int | — | 绑定生成模式生成开始时的活动版本，非负，重试保持原值 |
| window_revision | int | — | 绑定生成模式生成开始时的窗口版本，非负，重试保持原值 |

响应 200：

```json
{
  "injected": true,
  "injected_text": "[记忆浮现]\n…",
  "session_id": "s-weekend",
  "turn": 7,
  "memory": {"recalled_count": 3, "filtered_by_sleep": 0,
              "filtered_by_window": 1, "resolved_brought": []}
}
```

- `injected_text`：注入本轮 system 的召回文本。
- `memory`：Event / Entity 命中、来源过滤和语义匹配统计。
- `system_reminders`：需注入本轮的提醒数组。

仅做轮前召回可只传 `session_id` 和 `message`，不要求 `heart_turn_id`、消息 seq、版本号或先调用 Deliz。未传 `recall_id` 时不启用生成绑定及冻结响应重放；可选窗口过滤仍生效。若传 `recall_id`，仍须提供真实 `user_message_id`，遵守下面的生成绑定协议，不会因调用方是 CC 放宽来源校验。原文来源的配置与读取是 Ember 内部职责，不要求 HTTP 调用方承担原文同步。

`presentations` 记录呈现单位，`recall_refs` 记录触发会话、真实锚点 seq 和呈现事件的出处。只有事件正文/摘录进入 `recall_refs.events`；实体概览不展开隐藏成员。缺少可靠消息身份的事件保持 unresolved，路径由 app 解析。完整字段及限制见 [召回引用协议](PROTOCOL_DETAILS.md#recall-refs)。

### 4.1.1 生成绑定

turn_start 传入 recall_id 后，MCP 每次请求须带 X-Ember-Recall-Id 和 X-Ember-Tool-Call-Id，turn_end 须带同一 recall_id。新调用按活动生成校验，已完成工具回执可幂等重试。

GET /ember/generations/{recall_id} 查询生成状态；GET /ember/generations/{recall_id}/tools/{tool_call_id} 查询工具状态。原文读取回执不持久保存。

### 4.1.2 召回快照查询及 24 小时正文留存

同生成 done 后补交来源绑定：原 snapshot/response 冻结；source_candidates 可含 generation_completed 与 completion_bindings，实际变化推进 sources_revision。App 核验证据再做父链解析，不把新辅助字段透传 Heart。具体字段、兼容边界和幂等见 [来源完成协议](PROTOCOL_DETAILS.md#source-completion)。

`GET /ember/recalls/{recall_id}` 默认只返回冻结的呈现/引用元数据；`?view=response` 另带首次完整开轮响应的 response 字段。沿用 Bearer 和实例路由。具体字段和恢复边界见 [召回快照协议](PROTOCOL_DETAILS.md#recall-snapshots)。

同 recall_id 同业务内容的 start 在 24 小时内重放原响应，不重新选材或记账；异内容 409 id_conflict。计算/记账恢复中返回 202 recall_in_progress / recall_recovery_pending，Retry-After: 2；app 必须检查状态，不能把 202 当成成功空召回。未提交结果的中断返回 409 recall_outcome_unknown；生成登记缺少快照返回 409 recall_snapshot_unavailable；未知 ID 返回 404 unknown_recall。

正文从首次结果提交开始保留 24 小时，读取不续期；到期重复 start 和 response 查询返回 410 response_expired，默认 refs 仍可读，旧 ID 不可复用。启动/查询和每分钟后台任务清理临时正文；身份、引用、原有记忆及聊天保留。读取不恢复旧工具权限。主动 run 使用专用头，不因绑定失败而回退普通槽。

### 4.2 POST /ember/hooks/turn_end

收轮 + 补记回复行 id。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| session_id | string | — | |
| assistant_reply | string | ✓ | 最终回复 |
| turn | int | — | turn_start 返回的 turn 号，用于 stale 校验 |
| assistant_message_id | int | — | 本条回复在后端消息树的行 id |
| recall_id | string | 绑定生成模式必填 | 与 turn_start 相同的普通生成身份 |
| status | string | — | 绑定生成时为 done / error / cancelled；默认 done，需真实回复与 seq |

响应 200：`{"ok": true, "session_id": "…", "turn": 7}`

## 5. 普通 MCP 工具

POST /mcp/ 使用 MCP Streamable HTTP，tools/list 返回 22 个工具。所有普通生成调用沿用 X-Ember-Recall-Id / X-Ember-Tool-Call-Id 绑定，写入不借用其他窗口的活动槽。

| 工具 | 用途 |
|---|---|
| write_event / write_promise / revise_memory_event | 正式写 Event、约定及冷静期订正 |
| read_memory_source / search_memory | 完整原文下钻、主动检索 |
| pin_memory / unpin_memory / list_memory_pins / merge_memory_events | pin 分类管理及 10 分无 due 同义约定合并 |
| read_memory_slices / search_memory_slices | 原文切片下钻及检索 |
| write_memory_knowledge / review_memory_knowledge | 正式提炼认知/规律、审阅相似知识 |
| set_knowledge_pin / list_memory_knowledge | 知识 pin 与清单 |
| get_memory_extraction_batch / finish_memory_extraction_group | 提取材料批次及不写确认 |
| list_pending_promises / process_promise | 曝光约定处理及 pinned promise 主动 log |
| revise_entity_summary / read_entity_events | Entity 摘要订正及成员下钻 |
| read_relationship_point | 查询要点来源 Event 和时间 |

原文回执不写入持久工具账；进程重启后读取回执可能返回 result_not_retained。

### 5.1 Event 写入

write_event 与 write_promise 并排提供，来源二选一：顶层 quote_spans 或 source_slice_ids，不能同时提供。

两者顶层 event 都要求 summary、core、significance、kind、focus；significance 为 1–10 整数，focus 为来源区间逐字摘录的一句原话。write_event.kind 为 daily / relationship / project；write_promise.kind 固定为 promise，due 为带时区 ISO 日期时间或 null，初始 status=open、log=[] 由 Ember 生成。focus 与 quote_spans 使用相同匹配方式定位消息，内部只存解析后的消息 ID 供截断使用；读取不展示 focus。找不到或容错匹配歧义返回 event.focus 和重摘提示。

情况、理解、细节和感受写入 core。原文读取失败时 briefing 使用 core。

revise_memory_event 仅在原文完整保留于当前上下文、尚未截断的冷静期内可改 summary / core / significance / 非 promise 的 kind 及 recall_cues；一旦截断就锁住，后来再下钻或重新带入不解锁。来源绑定不可修改。约定状态与改期使用 process_promise。

write_event / write_promise 可选 supersedes 指向被取代的旧日常、项目、约定；旧内容保留，以 superseded_by 与取代关系退出自动召回。新旧任一端为 relationship 都拒绝此参数；关系只写新的接续。未知目标在任何写入前拒绝。

### 5.2 约定处理

list_pending_promises 返回真正进过上下文而尚未写 log 的约定及其原文 span、时间、进入路径；process_promise 要求 event_id 和非空 log，可选 status=done/dropped、due=带时区 ISO 日期时间或 null。普通未 pin 的未曝光约定不能通过该工具修改；pinned promise 可主动处理。写 log 后才确认处理完；状态不变也必须写。追加 log 与曝光回执可在重启后核对恢复。

到期仍 open 的约定追加在 briefing 末尾，不受预算裁切；未来到期或无 due 只按线索浮现，10 分无 due 写入时默认 pin。低分约定默认 <=7；完结后 Event 与其独占的切片退出自动召回，主动下钻仍可达。按 config/lifecycle.json 的时区每日零点检查到期满 lapse_days 的 open 约定，写入 lapsed log；启动补算。Windows 需安装 requirements.txt 中的 tzdata。待处理数量只在刷新 briefing 时更新，普通轮次不替换 briefing。

未 pin 且被 briefing 带入的约定会记曝光；list_pending_promises 读取曝光时的确切对话消息，已确认回复按同一 generation 补齐，不用 max-reply 推断。来源不可达会明确标注；这些原文结果只作易失工具响应，不落持久工具回执。

钉住的约定，包括默认钉住的 10 分无 due 约定，从任何入口进入上下文都不记曝光、不计待处理。process_promise 允许主动处理钉住的约定，无需曝光，仍要求非空 log，spans 可为空；普通未 pin 约定仍要求待处理曝光。

### 5.3 认知、规律与提取

普通 MCP 开放 write_memory_knowledge / review_memory_knowledge / set_knowledge_pin / list_memory_knowledge；置信度、切片来源与低置信度证据校验保留。review_memory_knowledge.group_id 使用已交付提取组的 group_id，组内附上的相似知识须逐条维持、改写或合并。

get_memory_extraction_batch() 不调用归组模型，只读取后台排好的最早材料：description、slices、similar_knowledge、group_id、part_id。批次材料数组按 JSON 字符数受 extraction.batch_chars 约束；整组放不下留到下一批，仅单组独自超限才切为 material_fragment，按 offset 连续拼接，total_chars 是完整材料长度。remaining_groups / remaining_chars 表示未包含在本批的剩余组数和字数；grouping_errors 表示归组错误数。

write_event、write_promise、write_memory_knowledge，以及 review_memory_knowledge 的 rewrite / merge 带 source_slice_ids 时，成功写入后自动将这些来源切片记为已处理并记录结果 ID。一组只使用部分切片时，其余切片仍留在原组，下次继续返回；全部处理后组完成。

finish_memory_extraction_group(group_id,source_slice_ids?,part_id?) 只表示不写。只传 group_id 跳过整组，source_slice_ids 指定组内几段切片，part_id 跳过已交付的超限分段；后两者不能同时提供。未处理材料继续返回。已撤出或已全部处理的组返回 ok，不存在的组报错。来源版本改变时，旧写入不会结掉新版本切片。

第三方只整理没有上级、有信息量且向量有效的切片，不从每轮召回 trace 建池，不代写正式记忆。后台按 extraction.worker_seconds 扫描，group_size / group_threshold / knowledge_threshold / similar_limit 也在 config/lifecycle.json。模型 purpose 为 slice_groups。提取原文回执易失，持久调用账不复制原文。

归组失败按种子切片版本退避，退避期内不作种子或候选，后续切片继续处理。extraction.retry_initial_seconds 和 retry_max_seconds 控制指数退避的初始时间及上限；failure_warning_count 控制连续失败告警阈值。健康检查返回 extraction_grouping_repeated_failure。成功归组、来源版本变化或切片已处理后清理对应失败记录。

### 5.4 Entity 与主题概括

Event 后台维护使用 config/models.json 的 memory_group / entity_identity / entity_summary / theme_summary 用途。向量只提供候选，LLM 判断一类事或具体对象。低分日常先复制不可变副本；够数后按全部主题副本重概括，用概括替换自动召回里的零散 Event，原件仍可主动检索。低分完结约定池形成同一层的客观主题概括；这层独立于认知/规律。聚合后的 daily 切片仍比原 Event 的 core，命中带出替代主题。

Entity 名称或别名命中只给 summary；Event 命中附所属 Entity 的 summary，卡片去重且不占 Event 三席。core、cue、切片和主题复用同一 embed 空间及每次输入的同一 query 向量。项目新事实可让旧 Event 退出自动召回，原件和出处不删除。

revise_entity_summary(entity_id,summary) 直接修订卡片并提交变化检查；read_entity_events(entity_id) 下钻具体 Event，再通过原文工具读取原话。Entity 只在新建、别名或 summary 变化时查重；确认同一对象才合并，保留较早卡片、别名、成员及旧 ID 映射。后台失败保留待重试状态，修订期间材料变化则重做，避免覆盖新修改。

### 5.5 Pin

list_memory_pins 按 relationship、ordinary、promise、knowledge 四类列出 pin。promise 类专指 10 分无 due 的约定，不限制条数，按总字数提醒；其余三类限制条数。pin_memory / set_knowledge_pin 满额返回 rejected / pin_class_full、该类清单与先 unpin 的提示。配置见 lifecycle.json 的 pin。

pin 原文先取完整来源，再围绕 focus 消息居中取 event_chars，省略两端分别标前面还有/后面还有。focus 本身不展示。认知/规律有单条字数保护；原件不改。read_memory_source 下钻返回完整原文。

merge_memory_events(event_ids,summary,core) 合并意思相同的 10 分无 due 约定，保留写入最早一条；同秒按库中创建顺序判先后。原文来源可跨会话，保留各自冻结消息身份，不拼成虚假的连续区间；log 只追加合入，原条不删，标 merged_into 并保留旧 ID 到主条映射。旧 ID 读取、pin 和处理约定会到主条。

钉住约定总字数超预算时，下一次 turn_start 响应给 system_reminders 数组；App 按该 hook 输出作为 system reminder 注入，无需另行通知 Ember。每次超额只提醒一次，整理到预算内后重新超出可再提醒。提醒和召回响应一起持久化，快照失败不会误消耗；同 recall_id 重试重放同一响应。提醒不写进持久 briefing。

### 5.6 关系时间线

关系 Event 后台逐件压一份，pin 住的也缓存压缩版；原件不删除。关系只挂 continues / responds_to 边，不相互取代，不提 Entity。拼装时按总量上下限，从早到晚逐件换压缩版；压缩区超限再按批抽点。要点只追加新文字或“后来……”；每条存来源 Event 和可能断开的时间区间。briefing 只放要点文字，read_relationship_point(point) 可按完整要点文字或 point_id 返回来源 Event 和时间，再下钻原话。

关系要点只追加，永不因 pin 作废或重建。已抽点的关系事件再被 pin 时，要点保留，该事件在自己的时间线位置额外给原样及原文；unpin 后只撤掉这份额外原样。抽点只收原文已不在当前窗口的历史事件；抽点后修订持续锁定。尚未抽点的冷静期订正只重压该件。

POST /ember/briefing/refresh 等待时间线上尚未完成的压缩和抽点，真正模型失败并重试仍失败时，未压缩条保留原样、未抽点批保留压缩版；内容保持准确，可能暂时超预算。后台继续重试，普通轮次不替换 briefing。只有当前 briefing 中被压缩/抽点的关系 Event 参与自动召回；原样在场的不重复召回，关系切片不作为入口。

关系预算、压缩区预算、上下限、抽点批大小、失败重试次数和 5:5 比例阈值均在 config/lifecycle.json。check_memory_health.py 通过 SQLite 副本检查时间线比例、压缩/抽点失败和超预算，不调用模型。relationship_timeline_budget_ratio 表示应调高关系时间线预算。

关系时间线后台按持久待办执行：关系事件写入、关系 pin 变化、失败重试到期触发；启动扫描恢复一次，空闲时不全量 synchronize。briefing 显式 refresh 仍等待时间线准备。

## 6. Sleep 边界

Sleep 只做梦和写信，由 App 管理。信的正文和版本都归 App，Ember 不参与，也不关联 Event 或维护证据账本。提取与约定处理使用普通 MCP。

以下入口返回 HTTP 410、`status=retired`，不产生写入：

| 入口 | code |
| --- | --- |
| POST /ember/sleep/knowledge/{operation} | legacy_sleep_step_retired |
| GET /ember/sleep/link/batches | legacy_sleep_step_retired |
| GET /ember/sleep/link/progress | legacy_sleep_step_retired |
| POST /ember/sleep/link/submit | legacy_sleep_step_retired |
| POST /ember/sleep/structure/run | legacy_sleep_step_retired |
| POST /ember/sleep/sediment/evidence | legacy_sediment_evidence_retired |

沉淀证据入口的 hint 为 `Sediment is now an App-only letter with no evidence ledger.`。实例目录中的 `sediment.jsonl` 不读、不写、不删除。

## 7. 原文提供方接口

### 7.1 精确路径

精确路径客户端按真实消息 ID 核验来源，不使用最大回复号或区间推断。混合分支历史候选最多在 64 次请求预算内分割校验；连接和鉴权错误不分割重试。

错误码：source_unconfigured、source_unreachable、source_auth_failed、source_instance_unknown、source_session_unknown、source_identity_missing、source_path_unknown、source_unavailable、source_read_limit、quote_not_found。

App 消息树是原文来源；精确下钻按需拉取，增量流的切片副本与派生缓存保存在 Ember 实例目录。请求：

```text
GET {EMBER_SOURCE_URL}/api/source/path?session_id={sid}&message_ids=71,75,80,82
Authorization: Bearer <EMBER_SOURCE_TOKEN>
X-Heart-Instance: <Ember 实例名>
```

`message_ids` 为可信的全局 seq 字符串集合，非空、去重、按数字升序；不能从旧账本的 min/max 或最大回复号推导。提供方逐条校验会话、原文资格及同一真实父链，可以读取旧分支且不依赖当前 leaf，不展开未请求的中间消息。响应为 `{"messages":[{"message_id":"71","role":"user","content":"原文"}, ...]}`，按 seq 升序、完整返回所请求编号；Ember 再校验编号、数量、顺序、角色与正文类型，不接受部分结果。

事件翻原文、召回内联、briefing 原文块使用该事件冻结的逐轮编号及可信完成绑定；事件内容版本变化、缺编号或缺可信出处返回 `not_archived`。不会用当前内存、当前 leaf 或邻近轮次替代；没有邻近路径证据时 `context_expanded=false`，仅取已证明的来源轮。

写记忆的 quote_spans / focus 定位、Event 原文下钻和约定曝光原文均通过精确路径客户端。历史语料取自实例内已持久生成的原始开轮/终态身份，不从旧区间账本推断；切片来源写入则核验 source_slice_ids 对应的来源版本。整份路径身份须由提供方校验；重新生成/编辑后的混合路径或编号归属不明时，历史读取降级。切片增量使用 /api/source/increment。

未配置、超时、401/404/409/其他非 200、响应残缺/错序/角色冲突均视为不可用；不回退 `/api/source`。缺少消息编号或只有最大回复号的记录不作为可信来源。未携带消息编号的 hook 可核对进程内文本，但写入 focus 仍要求可核验的来源消息 ID。

配置为 EMBER_SOURCE_URL、EMBER_SOURCE_TOKEN、EMBER_SOURCE_TIMEOUT。超时单位为秒，默认 8。原文提供方须校验实例与会话归属。

### 7.2 增量流

Ember 在 Event 落库后后台拉取，以及 POST /ember/briefing/refresh 时同步兜底。turn_end 提交生成完成及来源身份。GET /ember/slices/jobs/{job_id} 查询已登记的切片任务。

原文提供方需要实现 GET /api/source/increment?cursor=<JSON 编码的不透明游标>&limit=<页大小>，鉴权与 X-Heart-Instance 同 /api/source/path。初始 cursor=null；返回 {cursor, has_more:boolean, turns:[{session_id, messages:[{message_id,role,content}], user_is_system?:boolean}]}。只列出已完成且由提供方验证历史分支路径的完整人机消息对，或 assistant 单条；系统用户消息由提供方显式标记。游标属于该实例的增量流，不能用当前叶节点或最大消息 ID 猜测历史分支。has_more=true 时游标必须前进；空页同样返回 cursor 与 has_more。

两个触发点共用一个持久游标和排他执行锁，切片以消息身份去重；绑定、筛选和向量写入成功才推进游标，失败保留游标供重试。briefing 返回 source_sync 状态；来源或模型不可达时保留准确的已有记忆，报告 unavailable。刷新接口现会等待原文拉取与嵌入，应设置适当请求超时。

有上级的切片仅在切片自身与上级 core 分别过门槛后带出符合 kind 规则的上级 Event；不直接呈现原文。无上级且有信息量的切片才直接呈现。阈值、典型废话、页大小等见 config/lifecycle.json，可用 EMBER_LIFECYCLE_CONFIG 指定替代文件；所有嵌入仍由 config/models.json 的 embed 用途和统一网关提供。

## 8. briefing

POST /ember/briefing/refresh 收 session_id、keep_from_message_id，可附精确 context_message_ids；新会话、截断和断线重进需要新简报时调用。先兜底同步原文增量、等待关系压缩/抽点，再按顺序保存完整快照：关系时间线；其他 pin 原文；近期非关系、非 promise 的 8–10 分 core / 1–7 分 summary；pin 的认知/规律；到期 open 约定及待处理数量。未 pin 的 10 分也进入 72 小时近期层，超过窗口不永久常驻。promise 的浮现规则优先，不通过近期层提前带入未到期或无 due 约定。

`session_id` 为必填非空字符串，允许 `cc-` 前缀；`keep_from_message_id` 和 `context_message_ids` 都可省略，只传 `{"session_id":"cc-example"}` 即可刷新。窗口参数使用已配置来源的消息编号，不依赖 Deliz 特有的 leaf、活动版本或截断回调。未提供窗口参数时不排除当前窗口来源。原文增量仍由 Ember 从配置的来源拉取；来源未配置或不可达时，`source_sync` 报告 `unavailable`，继续使用已有准确记忆拼装。

**当前耗时**：refresh 是同步完成后返回，可能超过 60 秒。它会等待同实例的刷新锁、原文同步锁和时间线后台工作；同步拉取原文的所有增量页并完成筛选、嵌入，再逐件压缩未完成的关系 Event，必要时分批抽点，最后读取 pin 原文并保存快照。耗时随积压页数、事件数、模型请求及锁等待增加，接口没有整个刷新请求的统一截止时间，也没有固定完成时长保证。

仓库当前默认配置中，原文单次请求超时为 8 秒（`EMBER_SOURCE_TIMEOUT` 可覆盖），嵌入单次为 30 秒；关系压缩、抽点单次模型请求为 90 秒，关系压缩每件最多尝试 3 次（`timeline.retries=2`）。这些是单次请求/处理的配置，不是整个 refresh 的耗时上限；实际值以运行配置为准。模型失败后的准确内容降级见 §5.6，`source_sync` 状态见 §7。`GET /ember/briefing` 只读取已有快照，不执行上述刷新工作。

近期层使用 total_chars 扣除不可砍部分后的剩余预算，从最低分、最旧条目开始移除；不会硬截 core。关系、pin、知识及到期约定保持各自既定规则；最后的到期约定和待处理数量不砍。快照带 revision / refreshed_at / 分层统计，完整文本原子保存。GET /ember/briefing 只读该快照，不动态拼接新 pin 的知识；无快照为 empty，损坏为 failed / briefing_snapshot_unreadable。对话中不替换已有 briefing。

### 8.1 降级简报

`POST /ember/briefing/fallback` 用于 refresh 超时或失败时取用已有记忆。请求与 refresh 相同：必填非空 `session_id`，可选 `keep_from_message_id`、`context_message_ids`；支持相同的实例路由和鉴权。最小请求为 `{"session_id":"cc-example"}`。参数非法返回 `status=failed`、`code=invalid_briefing_context`。

接口保留最近保存的快照原文，追加刷新时间之后写入且仍在 72 小时近期层内的非关系、非 promise、未 pin Event：8–10 分用 core，1–7 分用 summary，沿用近期层排序、日期和资格筛选。随后追加快照之后新到期的 open 约定，以及快照之后新写入但已经到期的 open 约定。已在快照时到期的旧约定不重复追加。新增部分开头为「以下为上次简报之后的新记忆，简报尚未完成刷新」。关系时间线、pin 原文、认知/规律及待处理数量沿用快照，不重新拼装。

窗口参数仅过滤新增部分；来源全部在当前窗口的新 Event 不重复放入，跨会话或只在窗口内保留部分来源的 Event 保留。未传窗口参数不按窗口排除。来源不完整或切片版本已变化时，保守保留，不据此认定原文已在窗口中。

完整快照、标记和全部新增正文合计不超过 `briefing.total_chars` 时全部展开。放不下时整批不展开，改为数量、全部 Event ID 和 `search_memory` / `read_memory_source` 查阅提示，不硬截 core。快照原文与未展开提示均保留；当这两部分自身超过预算时，返回文本可以超预算。

| 响应字段 | 含义 |
|---|---|
| status | `ok`；无快照为 `empty`；快照损坏为 `failed` / `briefing_snapshot_unreadable` |
| text / chars | 快照加新增正文或未展开提示；`chars` 为完整返回文本字符数 |
| degraded | `true`，标识此次使用降级接口，不表示 refresh 的运行状态 |
| snapshot_revision / revision | 基于的已保存快照 revision；无快照时 `snapshot_revision=null` |
| refreshed_at | 已保存快照的刷新时间，不是降级请求时间 |
| expanded_count / unexpanded_count | 此次新增记忆的展开数、未展开数，不计快照原文 |
| unexpanded | `[{"event_id":"…","reason":"total_chars_exceeded"}]`；全部展开时为 `[]` |

接口不调模型、不拉原文、不等待刷新锁、原文同步锁或时间线工作，不保存快照、不登记曝光或修改窗口。只读本地状态遇到数据库忙或不可读时立即沿用快照，新增候选不展开，`reason=local_state_unavailable`；这些候选尚未完成资格及窗口核验。没有快照时 `text=""`，展开数和未展开数均为 0。

`GET /ember/briefing` 只取快照；`POST /ember/briefing/fallback` 取快照并补充新增记忆；`POST /ember/briefing/refresh` 同步完成刷新并保存新快照。fallback 不启动、取消或替代 refresh；调用方管理 refresh 的等待与后台继续执行。

## 9. 消息坐标

外部消息坐标为 message_id，使用原文服务的全局 seq。内部 turn-N、Heart t-XXXXXX 通过 turn_ledger.jsonl 映射到 message_id；Event 本体不保存映射。

## 10. 配置

| 变量 | 说明 |
|---|---|
| EMBER_HOST / EMBER_SIDECAR_PORT / EMBER_TOKEN | 监听与鉴权；未设置 EMBER_TOKEN 时不拦截请求 |
| EMBER_DATA_ROOT | 记忆库根目录 = heart 的 `data/`：default 实例库住 `<root>/ember`，其余住 `<root>/instances/<名>/ember`；根目录已有 events.jsonl 时，default 沿用该目录 |
| EMBER_INSTANCES | 实例清单，逗号分隔；缺省 `default`，清单外实例 404 |
| EMBER_DATA_DIR | 未设 EMBER_DATA_ROOT 时的根目录回落值，默认 `<项目>/data/` |
| EMBER_SOURCE_URL / EMBER_SOURCE_TOKEN / EMBER_SOURCE_TIMEOUT | 原文服务地址、鉴权和超时，见 §7.1 |
| config/models.json 中各 purpose 的 key_env | 模型与端点集中配置，key 只填对应 .env 变量 |
| EMBER_LIFECYCLE_CONFIG | 非秘密阈值/预算配置文件，缺省 config/lifecycle.json |

## 11. 主动 run

协议版本：`ember-runs/1`。

POST /ember/runs/start、POST /ember/runs/finish、GET /ember/runs/{run_id} 提供主动 run 的生命周期接口。当前只开放 read_memory_source，不支持写入或有副作用的 search_memory。字段、幂等、终态、重启和 MCP 绑定见 [主动 run 协议](PROTOCOL_DETAILS.md#runs)。
