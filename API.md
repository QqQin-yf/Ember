# ember 接口文档

> ember：旁路记忆系统。职责边界：记忆写入（模型自主+校验）、召回注入、异步维护与独立提取、沉淀证据账本；
> **不管**对话主路径——ember 挂了/未部署，聊天照常，只损失记忆能力。
> 消费方：app（钩子组 + sleep 编排 + 沉淀证据转交）、模型的工具循环（MCP 工具组）。
> （梦已归 app 的 dream_submit 工具，ember 不再向 heart 推送——旧梦通道 2026-10-15 退役。）

## 1. 基础信息

- 协议：HTTP/1.1 + JSON
- 默认地址：`127.0.0.1:8767`（sidecar 形态）
- 鉴权：配置 `EMBER_TOKEN` 后所有端点要求 `Authorization: Bearer <token>` 精确匹配（MCP 与钩子一视同仁）；未配置放行。令牌**全局不分实例**（一扇门多颗心），所有实例共用同一值
- 实例路由（2026-10-17，一颗心一套记忆库）：所有端点按 `X-Heart-Instance` 头（或 `?inst=` 查询参数）路由到对应实例，与 heart 同款——未带 = `default`，清单外实例 404。每个实例独立的记忆库，目录挂在该实例的 heart 数据目录下（见 §10 EMBER_DATA_ROOT）
- 接入形态：旁路（2026-10-12 拍板）——前端拥有对话循环，ember 只被钩子调用与 MCP 工具调用

## 2. 通用约定

### 2.1 失败如实回执，绝不编造

工具调用/核对失败一律返回 `{"status": "failed", "error": "<原因>"}`——不猜测、不兜底、不编造数据。引用的句子在语料里找不到就拒绝写入（双盲纪律）。

### 2.2 stale 拒收

旧模式用户连发消息时，慢到的旧轮 `turn_end` 会被拒收（`{"ok": false, "reason": "stale turn_end …"}`）。带 recall_id 的新模式允许补记原生成的真实历史终态，但不会恢复旧槽或污染当前语料，见 GENERATION_BINDING_SPEC.md。turn 计数允许留洞。

### 2.3 窗口过滤的降级语义

召回起点过滤依赖 app 传入的 `context_start_id`：缺省/无法换算 → **不过滤**（保守全召回）。事件的出处区间未回填 → 保守保留。宁可多召回，不漏。

## 3. 【时序契约】

```
普通轮：
  1. hook_turn_start（开轮 + 召回，返回注入文本）← 先于 LLM 调用
  2. app 调 LLM（召回文本由 app 自行注入当轮 system）
  3. hook_turn_end（收轮 + 补记回复行 id）        ← 后于 LLM 调用
写入（模型在对话中自主触发，经 app 的工具循环转 MCP）：
  · write_event / write_promise 在「进行中的一轮」存在时可用
```

- turn_end 的 turn 校验：与登记号不匹配则拒绝
- 新生成替换旧活动上下文；同 recall_id 同内容重试只重放快照，不开新轮

## 4. 钩子组（消费方：app）

### 4.1 POST /ember/hooks/turn_start

开新一轮 + 本地召回，返回记忆注入文本。

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| session_id | string | — | 会话标识（缺省默认会话） |
| message | string | ✓ | 本轮用户消息 |
| heart_turn_id | string | — | heart 的 `t-XXXXXX`（对账映射） |
| user_message_id | int/string | 新模式必填 | 真实全局 seq，不是消息 UUID（写入记来源） |
| context_start_id | int/string | — | 当前窗口起点 seq：来源全在窗口内的记忆不再召回 |
| recall_id | string | — | 稳定普通生成身份；启用时 session_id 也必填，重试原样携带 |
| occurred_at | string | — | 原始发生时间，需带时区；新模式固定保存 |
| activity_revision | int | — | 新模式生成开始时的活动版本，非负，重试保持原值 |
| window_revision | int | — | 新模式生成开始时的窗口版本，非负，重试保持原值 |

响应 200：

```json
{
  "injected": true,
  "injected_text": "[记忆浮现]\n…",
  "session_id": "s-weekend",
  "turn": 7,
  "memory": {"recalled_count": 3, "filtered_by_sleep": 0,
              "filtered_by_window": 1, "resolved_brought": [], …}
}
```

- `injected_text`：app **自行注入**当轮 system（ember 不改写任何请求）
- `memory` 为观察统计，前端可不消费

2026-10-20 本段新增 `presentations`（有序呈现粒度）和 `recall_refs`（触发会话、真实锚点 seq 字符串及实际输出事件的出处辅助）。只有事件正文/摘录进入 `recall_refs.events`；实体概览不展开隐藏成员。旧事件或缺可靠消息身份保持 unresolved，路径由 app 解析。完整字段及限制见 [RECALL_REFS_SPEC.md](PROTOCOL_DETAILS.md#recall-refs)。

带 recall_id 的新模式已本地实现持久快照与 24 小时原响应重放，见下节。主动 run 的隔离读接口已实现，见下文。原文客户端已迁移到精确路径，见 §7.1 与 SOURCE_PATH_VALIDATION.md；生产提供方配置和实例归属核验仍待完成，生产开关未批准。

### 4.1.1 普通生成显式绑定（2026-10-20 本段）

turn_start 可传 recall_id，MCP 随后逐请求带 X-Ember-Recall-Id 和 X-Ember-Tool-Call-Id，turn_end 必须带回同一 recall_id。新调用按活动生成校验，完成工具回执可持久重试；不再借共享当前槽。新增 GET /ember/generations/{recall_id} 及其 /tools/{tool_call_id} 查询。完整字段、错误码、重启与工具读取回执不持久保存规则见 GENERATION_BINDING_SPEC.md（本地历史记录）。

### 4.1.2 召回快照查询及 24 小时正文留存

同生成 done 后来源完成绑定已实现：原 snapshot/response 冻结；source_candidates 可含 generation_completed 与 completion_bindings，实际变化推进 sources_revision。App 核验证据再做父链解析，不把新辅助字段透传 Heart。具体字段、兼容边界和幂等见 [SOURCE_COMPLETION_SPEC.md](PROTOCOL_DETAILS.md#source-completion)。

`GET /ember/recalls/{recall_id}` 默认只返回冻结的呈现/引用元数据；`?view=response` 另带首次完整开轮响应的 response 字段。沿用 Bearer 和实例路由。具体字段和恢复边界见 [RECALL_SNAPSHOT_SPEC.md](PROTOCOL_DETAILS.md#recall-snapshots)。

同 recall_id 同业务内容的 start 在 24 小时内重放原响应，不重新选材或记账；异内容 409 id_conflict。计算/记账恢复中返回 202 recall_in_progress / recall_recovery_pending，Retry-After: 2；app 必须检查状态，不能把 202 当成成功空召回。未提交结果的中断返回 409 recall_outcome_unknown；旧版无快照登记返回 409 recall_snapshot_unavailable；未知 ID 返回 404 unknown_recall。

正文从首次结果提交开始保留 24 小时，读取不续期；到期重复 start 和 response 查询返回 410 response_expired，默认 refs 仍可读，旧 ID 不可复用。启动/查询和每分钟后台任务清理临时正文；身份、引用、原有记忆及聊天保留。读取不恢复旧工具权限。主动 run 使用专用头，不因绑定失败而回退普通槽。

### 4.2 POST /ember/hooks/turn_end

收轮 + 补记回复行 id。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| session_id | string | — | |
| assistant_reply | string | ✓ | 最终回复 |
| turn | int | — | turn_start 返回的 turn 号（stale 校验） |
| assistant_message_id | int | — | 本条回复在后端消息树的行 id |
| recall_id | string | 新模式必填 | 与 turn_start 相同的普通生成身份 |
| status | string | — | 新模式 done（默认）/error/cancelled；done 需真实回复与 seq |

响应 200：`{"ok": true, "session_id": "…", "turn": 7}`

## 5. 普通 MCP 工具

POST /mcp/ 使用 MCP Streamable HTTP，tools/list 返回 22 个工具。schema 由运行代码生成，本地完整字段与成功/失败回执可运行 scripts/export_tool_contracts.py 导出。所有普通生成调用沿用 X-Ember-Recall-Id / X-Ember-Tool-Call-Id 绑定，写入不借用其他窗口的活动槽。

| 工具 | 用途 |
|---|---|
| write_event / write_promise / revise_memory_event | 正式写 Event、约定及冷静期订正 |
| read_memory_source / search_memory | 完整原文下钻、主动检索 |
| pin_memory / unpin_memory / list_memory_pins / merge_memory_events | pin 分类管理及 10 分无 due 同义约定合并 |
| read_memory_slices / search_memory_slices | 原文切片下钻及检索 |
| write_memory_knowledge / review_memory_knowledge | 正式提炼认知/规律、审阅相似知识 |
| set_knowledge_pin / list_memory_knowledge | 知识 pin 与清单 |
| get_memory_extraction_batch / finish_memory_extraction_group | 可继续或停止的提取材料批次及结论 |
| list_pending_promises / process_promise | 曝光约定处理及 pinned promise 主动 log |
| revise_entity_summary / read_entity_events | Entity 摘要订正及成员下钻 |
| read_relationship_point | 查询要点来源 Event 和时间 |

详细内容和来源要求见下文 2026-11-01 各节。create_memory_event / write_event_from_slices 已取消，不再作为 MCP 名称；旧 Sleep 的 list_memory_groups / finish_memory_group / extract_memory_entity / set_slice_recall_state 不公开，由独立供料、后台维护和普通工具承担相应职责。原文回执不写入持久工具账；读取在进程重启后可能返回 result_not_retained。

## 6. Sleep 边界

Sleep 仅保留做梦与沉淀。POST /ember/sleep/knowledge/start、/tool、/finish 退役，返回 410 legacy_sleep_step_retired；原有 GET /ember/sleep/link/batches、/progress 以及 POST /ember/sleep/link/submit、/ember/sleep/structure/run 同样 410，无写入副作用。提取、约定处理改用普通 MCP，不需要 sleep_id 或 Sleep 专属上下文。

### 6.5 POST /ember/sleep/sediment/evidence

沉淀证据接收。**只在 app 真的存下一版新沉淀时调用**（模型交了证据、app 存了新版正文）；模型交空或没证据时 app 不收版、也不调本接口。

请求体：

```json
{"version": 3, "evidence_event_ids": ["EVT-…", "EVT-…"]}
```

- `version`：沉淀版本号，**照 app 侧为准**（ember 不校验连续性）；同版本重交 → 覆盖为最新一次的列表
- `evidence_event_ids`：本版证据 event id。**ember 做去重**：本版内重复、以及历史版本已有的 id 都剔掉，只挂净新增

响应 200（三种）：

```json
{"status": "appended", "version": 3,
 "evidence_kept": ["EVT-…"], "duplicates_dropped": ["EVT-…"],
 "latest_version": 3}
```

```json
{"status": "no_new_evidence", "version": 3,
 "duplicates_dropped": ["EVT-…"],
 "note": "全部 id 均为历史版本已有证据，本版不落账"}
```

```json
{"status": "rejected", "errors": ["不存在的事件 id（幻觉）: EVT-…"]}
```

- 校验：id 必须真实存在（库里有这个 event），编造即整条拒收
- 账本 `sediment.jsonl` 只追加（同版本覆盖除外），旧版本证据永不删——证据链查询（版本 → id → span → 原文）是后续读侧的入口
- 沉淀正文不经过 ember；换窗/briefing 需要正文时由 app 提供

## 7. ember 作为客户端

### 7.1 原文提供接口（精确路径）

2026-10-23 兼容修正：日常旧 hook 模式无需 recall_id 也可使用原始逐条编号，由精确路径提供方对照本轮真实锚点核验；不使用派生 max-reply 或范围推断。混合分支的历史候选可在 64 次请求预算内分割校验并保留合法部分，连接/鉴权错误不分割重试。span/出处/focus 复用同份语料。失败回执区分 source_unconfigured、source_unreachable、source_auth_failed、source_instance_unknown、source_session_unknown、source_identity_missing、source_path_unknown、source_unavailable、source_read_limit 与 quote_not_found；不再统一要求重摘。详见 QUOTE_SOURCE_COMPAT_FIX.md。

App 消息树是原文来源；精确下钻按需拉取，增量流的切片副本与派生缓存保存在 Ember 实例目录。所有远程消费方已切到：

```text
GET {EMBER_SOURCE_URL}/api/source/path?session_id={sid}&message_ids=71,75,80,82
Authorization: Bearer <EMBER_SOURCE_TOKEN>
X-Heart-Instance: <Ember 实例名>
```

`message_ids` 为可信的全局 seq 字符串集合，非空、去重、按数字升序；不能从旧账本的 min/max 或最大回复号推导。提供方逐条校验会话、原文资格及同一真实父链，可以读取旧分支且不依赖当前 leaf，不展开未请求的中间消息。响应为 `{"messages":[{"message_id":"71","role":"user","content":"原文"}, ...]}`，按 seq 升序、完整返回所请求编号；Ember 再校验编号、数量、顺序、角色与正文类型，不接受部分结果。

事件翻原文、召回内联、briefing 原文块使用该事件冻结的逐轮编号及可信完成绑定；事件内容版本变化、缺编号或缺可信出处返回 `not_archived`。不会用当前内存、当前 leaf 或邻近轮次替代；没有邻近路径证据时 `context_expanded=false`，仅取已证明的来源轮。

写记忆引文/定位和观察/sleep 通过同一精确客户端：历史语料取自实例内已持久生成的原始开轮/终态身份，不从旧区间账本推断。整份身份集合须由提供方校验在同一路径；若包含重新生成/编辑后的混合路径或重复编号归属不明，历史读取降级。写入只可使用仍能核验的当前真实用户原文；观察不推进游标、不制造事件。恢复混合会话的增量观察需要额外明确路径输入，暂不猜测。

未配置、超时、401/404/409/其他非 200、响应残缺/错序/角色冲突均视为不可用；不重试轰炸，不回退 `/api/source`。旧无编号 Event/只有最大回复号的账本不自动升级成可信来源。旧无编号 hook 仍可核对自身易失文本，但这不能成为可解析的历史路径证据。

配置仍为 `EMBER_SOURCE_URL`、`EMBER_SOURCE_TOKEN`、`EMBER_SOURCE_TIMEOUT`（秒，默认 8）。生产提供方必须核验实例与会话/库的归属；仅发送 `X-Heart-Instance` 不等于源端已经执行隔离。当前 App 接口忽略该头，真实多实例生产验收尚未关闭，配合清单见 PRODUCTION_PREREQUISITES.md。

## 8. briefing

POST /ember/briefing/refresh 显式刷新，GET /ember/briefing 只读冻结快照。refresh 会等待原文增量兜底、时间线压缩和抽点，超时需覆盖实际模型重试时长；拼装本身不调 LLM。精确字段与五档规则见下文“五档 briefing 快照”，完整生命周期见 MEMORY_LIFECYCLE_SPEC.md。对话中途不自动替换快照。

## 9. 坐标系说明

对外契约**只有 message_id 一套**（后端消息树行 id）：钩子请求携带、事件出处经账本换算回指。

内部坐标（app 无需关心）：ember 的 `turn-N`（会话内序数）与 heart 的 `t-XXXXXX` 各自独立发号、经 `turn_ledger.jsonl` 旁路账本互相映射并指向 message_id——换算是 ember 内部事务，Event 本体不存这些映射（旁路文件，可重建）。

## 10. 配置清单（.env，启动时自动加载）

| 变量 | 说明 |
|---|---|
| EMBER_HOST / EMBER_SIDECAR_PORT / EMBER_TOKEN | 监听与鉴权（跨机部署 EMBER_TOKEN 必设，不设 = 不拦） |
| EMBER_DATA_ROOT | 记忆库根目录 = heart 的 `data/`：default 实例库住 `<root>/ember`，其余住 `<root>/instances/<名>/ember`（旧扁平库有 events.jsonl 时 default 原样沿用，零迁移） |
| EMBER_INSTANCES | 实例清单，逗号分隔（缺省 `default`）；清单外实例 404 |
| EMBER_DATA_DIR | （旧变量）未设 EMBER_DATA_ROOT 时作为根目录回落；默认 `<项目>/data/` |
| EMBER_SOURCE_URL / EMBER_SOURCE_TOKEN / EMBER_SOURCE_TIMEOUT | 精确路径原文接口（Deliz `/api/source/path`；实例归属/鉴权部署核验见 §7.1） |
| config/models.json 中各 purpose 的 key_env | 模型与端点集中配置，key 只填对应 .env 变量 |
| EMBER_LIFECYCLE_CONFIG | 非秘密阈值/预算配置文件，缺省 config/lifecycle.json |


## 主动 run 隔离接入（ember-runs/1）

新增 `POST /ember/runs/start`、`POST /ember/runs/finish`、`GET /ember/runs/{run_id}`。严格字段、幂等/终态/重启与 MCP 绑定见 [RUNS_SPEC.md](PROTOCOL_DETAILS.md#runs)，App 接线回执见 APP_WAKE_RUNS_REPLY.md（本地历史记录）。首版仅开放 read_memory_source；不支持写入工具或有副作用的 search_memory。当前仅工作区实现和隔离验收，不代表日常主动发送已开启。


## 2026-11-01 Event 写入契约

本节替代旧的 create_memory_event / write_event_from_slices 字段说明；这两个 MCP 名称已取消。write_event 与 write_promise 并排提供，来源二选一：顶层 quote_spans 或 source_slice_ids，不能同时提供。

两者顶层 event 都要求 summary、core、significance（1–10 整数）、kind、focus（从来源区间逐字摘录的一句原话）。write_event.kind 为 daily / relationship / project；write_promise.kind 固定为 promise，due 为带时区 ISO 日期时间或 null，初始 status=open、log=[] 由 Ember 生成。focus 与 quote_spans 使用相同匹配方式定位消息，内部只存解析后的消息 ID 供截断使用；读取不展示 focus。找不到或容错匹配歧义返回 event.focus 和重摘提示。

key_quotes、objects、participants、context、state、outcome、affect、details 均已删除；理解、情况、细节和感受写入 core。原文读取失败时 briefing 只保留 core。旧测试数据不迁移。

revise_memory_event 仅在原文完整保留于当前上下文、尚未截断的冷静期内可改 summary / core / significance / 非 promise 的 kind 及 recall_cues；一旦截断就锁住，后来再下钻或重新带入不解锁。来源绑定不可修改。约定状态与改期由后续的约定处理工具承担。


### 原文增量提供方契约（2026-11-01）

Ember 在 Event 落库后后台拉取，以及 POST /ember/briefing/refresh 时同步兜底；不再需要 App 另行通知切片。turn_end 仅提交生成完成及来源身份，不再启动切片任务。旧 /ember/slices/jobs 仅能查询历史任务。

原文提供方需要实现 GET /api/source/increment?cursor=<JSON 编码的不透明游标>&limit=<页大小>，鉴权与 X-Heart-Instance 同 /api/source/path。初始 cursor=null；返回 {cursor, has_more:boolean, turns:[{session_id, messages:[{message_id,role,content}], user_is_system?:boolean}]}。只列出已完成且由提供方验证历史分支路径的完整人机消息对，或 assistant 单条；系统用户消息由提供方显式标记。游标属于该实例的增量流，不能用当前叶节点或最大消息 ID 猜测历史分支。has_more=true 时游标必须前进；空页同样返回 cursor 与 has_more。

两个触发点共用一个持久游标和排他执行锁，切片以消息身份去重；绑定、筛选和向量写入成功才推进游标，失败保留游标供重试。briefing 返回 source_sync 状态；来源或模型不可达时保留准确的已有记忆，报告 unavailable，不假装增量已同步。刷新接口现会等待原文拉取与嵌入，应设置适当请求超时。App 原文服务适配此接口后再启用新链路。

有上级的切片仅在切片自身与上级 core 分别过门槛后带出符合 kind 规则的上级 Event；不直接呈现原文。无上级且有信息量的切片才直接呈现。阈值、典型废话、页大小等见 config/lifecycle.json（EMBER_LIFECYCLE_CONFIG 可指定替代文件）；所有嵌入仍由 config/models.json 的 embed 用途和统一网关提供。


### 约定处理（2026-11-01）

list_pending_promises 返回真正进过上下文而尚未写 log 的约定及其原文 span、时间、进入路径；process_promise 要求 event_id 和非空 log，可选 status=done/dropped、due=带时区 ISO 日期时间或 null。普通未 pin 的未曝光约定不能通过该工具修改；pinned promise 可主动处理。写 log 后才确认处理完；状态不变也必须写。追加 log 与曝光回执可在重启后核对恢复。

到期仍 open 的约定追加在 briefing 末尾，不受预算裁切；未来到期或无 due 只按线索浮现，10 分无 due 写入时默认 pin。低分（默认 <=7）完结后 Event 与其独占的切片退出自动召回，主动下钻仍可达。按 config/lifecycle.json 的时区每日零点检查到期满 lapse_days 的 open 约定，写入 lapsed log；启动补算。Windows 需安装 requirements.txt 中的 tzdata。待处理数量只在刷新 briefing 时更新，普通轮次不替换 briefing。


### 共用归组、Entity 与主题概括（2026-11-01）

Event 后台维护使用 config/models.json 的 memory_group / entity_identity / entity_summary / theme_summary 用途。向量只提供候选，LLM 判断一类事或具体对象。低分日常先复制不可变副本；够数后按全部主题副本重概括，用概括替换自动召回里的零散 Event，原件仍可主动检索。低分完结约定池形成同一层的客观主题概括；这层独立于认知/规律。聚合后的 daily 切片仍比原 Event 的 core，命中带出替代主题。

Entity 名称或别名命中只给 summary；Event 命中附所属 Entity 的 summary，卡片去重且不占 Event 三席。Direction 不再参与自动召回。core、cue、切片和主题复用同一 embed 空间及每次输入的同一 query 向量。项目新事实可让旧 Event 退出自动召回，原件和出处不删除。

revise_entity_summary(entity_id,summary) 直接修订卡片并提交变化检查；read_entity_events(entity_id) 下钻具体 Event，再通过原文工具读取原话。Entity 只在新建、别名或 summary 变化时查重；确认同一对象才合并，保留较早卡片、别名、成员及旧 ID 映射。后台失败保留待重试状态，修订期间材料变化则重做，避免覆盖新修改。


### 关系时间线（2026-11-01）

关系 Event 后台逐件压一份，pin 住的也缓存压缩版；原件不删除。关系只挂 continues / responds_to 边，不相互取代，不提 Entity。拼装时按总量上下限，从早到晚逐件换压缩版；压缩区超限再按批抽点。要点只追加新文字或“后来……”；每条存来源 Event 和可能断开的时间区间。briefing 只放要点文字，read_relationship_point(point) 可按完整要点文字或 point_id 返回来源 Event 和时间，再下钻原话。

POST /ember/briefing/refresh 现在会等待时间线上尚未完成的压缩和抽点，不使用旧完成状态替代。真正模型失败并重试仍失败时，未压缩条保留原样、未抽点批保留压缩版；内容保持准确，可能暂时超预算。后台继续重试，普通轮次不替换 briefing。只有当前 briefing 中被压缩/抽点的关系 Event 参与自动召回；原样在场的不重复召回，关系切片不作为入口。

关系预算、压缩区预算、上下限、抽点批大小、失败重试次数和 5:5 比例阈值均在 config/lifecycle.json。check_memory_health.py 通过 SQLite 副本报告 relationship_timeline_budget_ratio（应调高关系时间线预算）、压缩/抽点失败和暂时超预算，不调用模型，不输出记忆内容。


### Pin 分类、原文与单次提醒（2026-11-01）

list_memory_pins 按 relationship / ordinary / promise（10 分无 due）/ knowledge 列出 pin。三类按条数上限限制；10 分无 due 约定不限制条数，按总字数提醒。pin_memory / set_knowledge_pin 满额返回 rejected / pin_class_full、该类清单与先 unpin 的提示。旧 1000-token pin 拒绝规则取消，配置见 lifecycle.json 的 pin。

pin 原文先取完整来源，再围绕 focus 消息居中取 event_chars，省略两端分别标前面还有/后面还有。focus 本身不展示。认知/规律有单条字数保护；原件不改。read_memory_source 下钻返回完整原文，不沿用旧 4000 字前缀限制。

merge_memory_events(event_ids,summary,core) 合并意思相同的 10 分无 due 约定，保留写入最早一条；同秒按库中创建顺序判先后。原文来源可跨会话，保留各自冻结消息身份，不拼成虚假的连续区间；log 只追加合入，原条不删，标 merged_into 并保留旧 ID 到主条映射。旧 ID 读取、pin 和处理约定会到主条。

钉住约定总字数超预算时，下一次 turn_start 响应给 system_reminders 数组；App 按该 hook 输出作为 system reminder 注入，无需另行通知 Ember。每次超额只提醒一次，整理到预算内后重新超出可再提醒。提醒和召回响应一起持久化，快照失败不会误消耗；同 recall_id 重试重放同一响应。提醒不写进持久 briefing。


### 五档 briefing 快照（2026-11-01）

POST /ember/briefing/refresh 收 session_id、keep_from_message_id，可附精确 context_message_ids；新会话、截断和断线重进需要新简报时调用。先兜底同步原文增量、等待关系压缩/抽点，再按顺序保存完整快照：关系时间线；其他 pin 原文；近期非关系、非 promise 的 8–10 分 core / 1–7 分 summary；pin 的认知/规律；到期 open 约定及待处理数量。未 pin 的 10 分也进入 72 小时近期层，超过窗口不永久常驻，旧“召回次数最多一条”规则取消。promise 的浮现规则优先，不通过近期层提前带入未到期或无 due 约定。

近期层使用 total_chars 扣除不可砍部分后的剩余预算，从最低分、最旧条目开始移除；不会硬截 core。关系、pin、知识及到期约定保持各自既定规则；最后的到期约定和待处理数量不砍。快照带 revision / refreshed_at / 分层统计，完整文本原子保存。GET /ember/briefing 只读该快照，不动态拼接新 pin 的知识；无快照为 empty，损坏为 failed / briefing_snapshot_unreadable。对话中不替换已有 briefing。

未 pin 且被 briefing 带入的约定会记曝光；list_pending_promises 读取曝光时的确切对话消息，已确认回复按同一 generation 补齐，不用 max-reply 推断。来源不可达会明确标注；这些原文结果只作易失工具响应，不落持久工具回执。

修正：write_event / write_promise 可选 supersedes 指向被取代的旧日常、项目、约定；旧内容保留，以 superseded_by 与取代关系退出自动召回。新旧任一端为 relationship 都拒绝此参数；关系只写新的接续。未知目标在任何写入前拒绝。

修正：关系要点只追加，永不因 pin 作废或重建。已抽点的关系事件再被 pin 时，要点保留，该事件在自己的时间线位置额外给原样及原文；unpin 后只撤掉这份额外原样。抽点只收原文已不在当前窗口的历史事件；抽点后修订持续锁定。尚未抽点的冷静期订正只重压该件。

钉住的约定（包括默认钉住的 10 分无 due）从任何入口进入上下文都不记曝光、不计待处理。process_promise 允许主动处理钉住的约定，无需曝光，仍要求非空 log，spans 可为空；普通未 pin 约定仍要求待处理曝光。

关系时间线后台按持久待办执行：关系事件写入、关系 pin 变化、失败重试到期触发；启动扫描恢复一次，空闲时不全量 synchronize。briefing 显式 refresh 仍等待时间线准备。

### 独立提取工具（2026-11-01，第 8 步）

普通 MCP 开放 write_memory_knowledge / review_memory_knowledge / set_knowledge_pin / list_memory_knowledge；置信度、切片来源与低置信度证据校验保留。review_memory_knowledge.group_id 使用已交付提取组的 group_id，组内附上的相似知识须逐条维持、改写或合并。

get_memory_extraction_batch() 不调用归组模型，只读取后台排好的最早材料：description、slices、similar_knowledge、group_id、part_id。批次材料数组按 JSON 字符数受 extraction.batch_chars 约束；整组放不下留到下一批，仅单组独自超限才切为 material_fragment，按 offset 连续拼接，total_chars 是完整材料长度。返回 remaining_groups / remaining_chars（不含本批材料）和 grouping_errors；模型可继续或停。

finish_memory_extraction_group(group_id,part_id,conclusion,result_ids?) 接受 written / merged / skipped。写了或并进旧的须给已存在的 Event/knowledge ID；不写可省 result_ids。全部分段给结论才完成组。未确认分段下次重现，重复相同结论幂等，冲突结论拒绝。原文版本改变会拒绝旧材料；切片后来被 Event 覆盖则退出提取候选。

第三方只整理没有上级、有信息量且向量有效的切片，不从每轮召回 trace 建池，不代写正式记忆。后台按 extraction.worker_seconds 扫描，group_size / group_threshold / knowledge_threshold / similar_limit 也在 config/lifecycle.json。模型 purpose 为 slice_groups。提取原文回执易失，持久调用账不复制原文。
