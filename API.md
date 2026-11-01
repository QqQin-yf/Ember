# ember 接口文档

> ember：旁路记忆系统。职责边界：记忆写入（模型自主+校验）、召回注入、异步维护与 Sleep 正式产出、沉淀证据账本；
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
  · create_memory_event 只在「进行中的一轮」存在时可用
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

带 recall_id 的新模式已本地实现持久快照与 24 小时原响应重放，见下节。主动 run 未实现。原文客户端已迁移到精确路径，见 §7.1 与 SOURCE_PATH_VALIDATION.md；生产提供方配置和实例归属核验仍待完成，生产开关未批准。

### 4.1.1 普通生成显式绑定（2026-10-20 本段）

turn_start 可传 recall_id，MCP 随后逐请求带 X-Ember-Recall-Id 和 X-Ember-Tool-Call-Id，turn_end 必须带回同一 recall_id。新调用按活动生成校验，完成工具回执可持久重试；不再借共享当前槽。新增 GET /ember/generations/{recall_id} 及其 /tools/{tool_call_id} 查询。完整字段、错误码、重启与工具读取回执不持久保存规则见 GENERATION_BINDING_SPEC.md（本地历史记录）。

### 4.1.2 召回快照查询及 24 小时正文留存

同生成 done 后来源完成绑定已实现：原 snapshot/response 冻结；source_candidates 可含 generation_completed 与 completion_bindings，实际变化推进 sources_revision。App 核验证据再做父链解析，不把新辅助字段透传 Heart。具体字段、兼容边界和幂等见 [SOURCE_COMPLETION_SPEC.md](PROTOCOL_DETAILS.md#source-completion)。

`GET /ember/recalls/{recall_id}` 默认只返回冻结的呈现/引用元数据；`?view=response` 另带首次完整开轮响应的 response 字段。沿用 Bearer 和实例路由。具体字段和恢复边界见 [RECALL_SNAPSHOT_SPEC.md](PROTOCOL_DETAILS.md#recall-snapshots)。

同 recall_id 同业务内容的 start 在 24 小时内重放原响应，不重新选材或记账；异内容 409 id_conflict。计算/记账恢复中返回 202 recall_in_progress / recall_recovery_pending，Retry-After: 2；app 必须检查状态，不能把 202 当成成功空召回。未提交结果的中断返回 409 recall_outcome_unknown；旧版无快照登记返回 409 recall_snapshot_unavailable；未知 ID 返回 404 unknown_recall。

正文从首次结果提交开始保留 24 小时，读取不续期；到期重复 start 和 response 查询返回 410 response_expired，默认 refs 仍可读，旧 ID 不可复用。启动/查询和每分钟后台任务清理临时正文；身份、引用、原有记忆及聊天保留。读取不恢复旧工具权限。主动 run 尚不支持，不因带 run 头而回退普通槽。

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

## 5. MCP 工具组（消费方：模型的工具循环，经 app 转发）

端点：`POST /mcp/`（MCP Streamable HTTP；建议带尾斜杠，`/mcp` 会被 307 重定向一次）。工具清单 `tools/list` 返回六件；以下为语义契约。

- 实例路由对 MCP 同样生效，且 `X-Heart-Instance` 头要带在**每个** MCP 请求上（initialize / tools/list / tools/call——路由逐请求解析，不是握手时定死）

### 5.1 create_memory_event

记录一条经历。**只在出现具体的、值得留存的经历时调用**；同一条经历只调一次（重复调用会产生重复记忆）。

必填参数：`event`（对象：summary/participants/objects/context/core/outcome/state/affect/significance/details/key_quotes）、`recall_routes`（召回线索）、`quote_spans`（出处句对：起尾句逐字摘自原文；**span 要还原整个场景**——从对话里刚谈到这个话题起，到话题结束止，不只圈提供信息量的那句话）。

**2026-10-17 起两个格式纪律（写入端硬校验）**：

- `significance` = `"N：一句话理由"`（N∈1-10：10=重大转折/永远要记；8-9=有实质影响的进展发现决定；5-7=有内容偏背景；1-4=琐碎，**不要调用本工具**——随口带过的偏好、习惯、单点琐事不写成长期记忆）。该分数是简报分层的依据
- `participants`：对话双方在场时双方都算——ai 说过话或被涉及就带上 `"ai"`，不要只写 user

校验与回执：

- `quote_spans` 全部定位不上 → **整条拒绝**（`failed`，指路重摘）——双盲纪律
- 出处定位先找原文子串，未命中时只保留文字和数字作连续子串匹配，忽略 emoji、Markdown 标记、标点和空白。容错只能唯一命中一条原文消息；仍保存该真实消息的坐标，不改写原文、不用语义相似或错字匹配。精确命中优先于容错候选。
- 容错有多个消息候选 → `failed / quote_ambiguous`，返回具体 `field`（如 `quote_spans[0].end_quote`）、`candidate_count` 和扩长摘录的建议；整条写入在落盘前拒绝，不能任选第一条或当作单端缺失降级。
- `key_quotes` 逐字核对，对不上的条目丢弃并进 `dropped_quotes`（记忆本体已保存，不因摘录出入重试）
- 出处（会话/轮次/heart 钉子/message_id）由进行中的一轮自动关联，模型无需提供

成功回执：`{"status": "created", "event_id": "EVT-…", "summary", "raw_ref", "route_count", "recall_cues" [, "dropped_quotes"]}`

#### 工具失败回执（2026-10-23）

MCP 的 schema 校验失败，以及业务返回 `status=failed`、`status=rejected` 或 `ok=false` 时，外层 `CallToolResult.isError=true`。`pending`、`in_progress`、`not_archived` 等状态本身不被改成失败。已有回执正文与稳定调用身份的重放规则保留。

写记忆的参数错误保留 `status=failed` / `error`，补充 `code=invalid_tool_arguments`、精确 `field` 和 `hint`。例如：

```json
{
  "status": "failed",
  "code": "invalid_tool_arguments",
  "field": "recall_routes[0].cue",
  "error": "validation failed: recall_routes[0].cue 必须是非空字符串",
  "hint": "请填写非空的召回关键词，不能只含空白字符。"
}
```

层级放错会指明 `event.recall_routes` 应移到顶层，不自动搬移或补全模型参数。原文读取失败保留各自的 `source_*` code 并提供处理建议；只有可用原文中的摘录不匹配才返回 `quote_not_found`，其 `field=quote_spans`。未配置、鉴权失败、会话/路径不明均不能通过改写摘录来绕过。

验收记录见 TOOL_ERROR_RELIABILITY.md（本地历史记录）。

### 5.2 read_memory_source

翻一条记忆对应的对话原文（只读）。只在需要核对原话细节时调用。语料 = 内存会话 ∪ 向后端原文接口按需拉取（用完即弃，本地不落盘）。

- 成功：`{"status": "ok", "event_id", "turns", "context_expanded", "transcript", "truncated"}`——**原文上下文扩展（10-17）**：事件覆盖单轮时自动扩 ±3 轮（前后不足就有多少拉多少，`context_expanded=true`）；真区间按区间
- 不可得：`{"status": "not_archived", "reason": …}`（原文接口未配置/区间拉不到）；id 未知 → `not_found`
- superseded（被修正）的事件也可读——显式翻看不是自动召回

### 5.3 revise_memory_event（冷静期修正）

修改一条**自己写下的**记忆。只在用户当场指出记忆内容与事实不符时调用。

- **冷静期** = 该记忆尚未进入夜间维护（sleep 维护）→ 直改本尊（此时派生物未建立，改了零残留）
- **过冷静期** → `{"status": "rejected", "reason": "…已定稿…请用 create_memory_event 写更正记忆"}`——写更正事件判 resolves，夜间维护会自动让旧记忆退出召回
- 可改字段（叙述类）：`summary / participants / objects / context / core / outcome / state / affect / significance / details` + `recall_cues`（整体重设召回线索）
- 锁死字段（显式传入即 `failed`）：`key_quotes / quote_spans / turn_refs / raw_ref / source_ref`——摘录与原文的绑定一动双盲核对就废，出处一动回指就断
- 无修改痕迹记录（改了就改了）

回执：`{"status": "revised", "event_id", "revised_fields": [...]}`

### 5.4 search_memory（主动检索）

按内容主动检索记忆库——**自动浮现之外的另一条路**：浮起有竞争有衰减，捞起无竞争无衰减。想确认某件事、找一段说不清位置的经历、按时间回顾时调用。

参数：`query`（必填，自然语言，不要求原文用词；**可含多个关键词**，空格/逗号分隔——语义主模态整串理解，关键词降级兜底时取并集并按命中数排序）+ `time_range`（可选，支持「昨天/上周/最近3天」相对表达或 ISO 起止 `a..b`）+ `k`（可选，默认 3 上限 5，超限钳制并告知——检索是定位器，超限说明 query 太泛）。

响应要点：

- **实体卡**（若查 query 与某实体的 summary 语义共振）：`{entity_id, name, status, summary, member_count, recent[3]}`——印象层先出，细节按 id 下钻
- **items**：`{event_id, summary, ingest_time, status, entity_id}`——**全库可达**：`status` 如实标注 `normal / faded（淡忘中）/ superseded（+`superseded_by` 修正者指针）`
- **relation + guidance**：命中形态 single / cluster / chain（chain = 共享同一实体）与对应行动建议；没有找到如实说记不清
- **检索即提取**：命中入提取账（与召回同权）——刷新遗忘计时器，淡忘中的条目即时解除收紧（note 会告知「已唤醒 N 条」）
- 向量服务不可达时自动降级关键词检索（note 告知）；`SEARCH_HIT_FLOOR=0.30` 为 baseline 待标定

### 5.5 pin_memory / unpin_memory（钉住，2026-10-17）

把一条记忆钉住/取消钉住（幂等）。**只在用户明确要求「钉住」「记住这事别忘」时调用**。

联动：钉住的**必进简报（原文层）**且**自动召回不再浮现**（已常驻在场，再浮是复读；search 主动检索不受影响）。HTTP 等价端点：`POST /ember/memory/pin` / `POST /ember/memory/unpin`。

回执：`{"status": "ok", "event_id", "pinned": true/false [, "already"|"was_pinned"]}`

## 6. Sleep正式产出与后台维护

关系判断、归类、资格标注由独立后台处理，不由App Sleep重复执行。Sleep读取冻结待办，App AI正式提取Entity、写Event/认知/规律，审阅合并/补写/维持并评分，以及pin/unpin；之后继续沉淀与dream。

### 6.1 POST /ember/sleep/knowledge/start

请求`{sleep_id,session_id}`，返回冻结的`groups`、`knowledge`、`existing_outputs`和工具schema。开始前统一聚合待办，普通补充不重开已处理方向。完整材料格式见[MEMORY_LIFECYCLE_SPEC.md](MEMORY_LIFECYCLE_SPEC.md)。

### 6.2 POST /ember/sleep/knowledge/tool

请求`{sleep_id,session_id,call_id,name,arguments}`。按call_id幂等；必须先读原文再正式写入，来源变动拒绝。普通MCP不提供正式提炼、认知/规律评分及pin管理工具。

### 6.3 POST /ember/sleep/knowledge/finish

请求`{sleep_id,session_id,state:completed|stopped}`。completed要求本批处理完；中断保留未处理材料。Sleep期间后台新证据暂存，结束后归组；旧Sleep重放不会拿到未来的新待办。

### 6.4 退役入口

`GET /ember/sleep/link/batches`、`GET /ember/sleep/link/progress`、`POST /ember/sleep/link/submit`和`POST /ember/sleep/structure/run`均返回HTTP 410及`legacy_sleep_step_retired`，没有写入副作用。

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

2026-10-23 兼容修正：日常旧 hook 模式无需 recall_id 也可使用原始逐条编号，由精确路径提供方对照本轮真实锚点核验；不使用派生 max-reply 或范围推断。混合分支的历史候选可在 64 次请求预算内分割校验并保留合法部分，连接/鉴权错误不分割重试。span/出处/key_quotes 复用同份语料。失败回执区分 source_unconfigured、source_unreachable、source_auth_failed、source_instance_unknown、source_session_unknown、source_identity_missing、source_path_unknown、source_unavailable、source_read_limit 与 quote_not_found；不再统一要求重摘。详见 QUOTE_SOURCE_COMPAT_FIX.md。

原文仍只存 App 消息树，Ember 按需读取、用完即弃，不持久保存正文。所有远程消费方已切到：

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

## 8. 简报（briefing，消费方：app 截断编排）

**2026-10-17 设计夜定稿。截断驱动、拼装式零 LLM**：上下文丢头留尾式截断，每次截断时 app 调 refresh 刷新简报并注入留下上下文的开头；旧简报随头部丢弃，新简报自包含。断线重进/新会话用 GET 取。

- `POST /ember/briefing/refresh`：体 `{"session_id", "keep_from_message_id"}`（现场起点的后端消息行 id——现场区间记忆不进，原文已在上下文；ember 经账本换算，app 不接触内部轮号）。返回 `{"status": "ok", "text", "chars", "stats": {…}}`，`text` 即注入文本
- `GET /ember/briefing`：取当前简报；从未刷新 → `{"status": "empty"}`
- 入选/分层/预算/去重全规则见 **BRIEFING_SPEC.md**（要点：pin 与 significance=10 必进给原文层；72h 新增 8-9 给 core、1-7 给 summary 一句；召回过的旧记忆只放最相关一条；现场与观察事件不进；预算 6000 字从低层砍，pin/10 永不砍；简报注入计一次在场——2h 内召回走轻量行）。每条条目显式标注 `[日期：YYYY-MM-DD]`（取存储的 event_time；缺失/null/空白显示 `[时间缺失]`，不可解析显示 `[时间解析失败]`，不以 ingest_time 兜底）。普通写记忆的 event_time 目前是写入时刻；已有缓存在下次正常 refresh 后采用新格式
- 钉住端点：`POST /ember/memory/pin` / `POST /ember/memory/unpin`（体 `{"event_id"}`，幂等；联动见 §5.5）

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
| EMBER_EMBED_BASE_URL / EMBER_EMBED_API_KEY / EMBER_EMBED_MODEL | 向量服务 |
| ZHIPU_API_KEY / EMBER_SLEEP_MODEL | 观察提炼/合并等任务的 LLM |
| EMBER_OWNER_NAME | 观察提炼注入 prompt 的用户称呼（不配写 "user"，见 §6 观察式记忆） |
| EMBER_BRIEFING_BUDGET | 简报字符硬顶（缺省 6000，见 §8） |


## 主动 run 隔离接入（ember-runs/1）

新增 `POST /ember/runs/start`、`POST /ember/runs/finish`、`GET /ember/runs/{run_id}`。严格字段、幂等/终态/重启与 MCP 绑定见 [RUNS_SPEC.md](PROTOCOL_DETAILS.md#runs)，App 接线回执见 APP_WAKE_RUNS_REPLY.md（本地历史记录）。首版仅开放 read_memory_source；不支持写入工具或有副作用的 search_memory。当前仅工作区实现和隔离验收，不代表日常主动发送已开启。


## 2026-11-01 Event 写入契约

本节替代旧的 create_memory_event / write_event_from_slices 字段说明；这两个 MCP 名称已取消。write_event 与 write_promise 并排提供，来源二选一：顶层 quote_spans 或 source_slice_ids，不能同时提供。

两者顶层 event 都要求 summary、core、significance（1–10 整数）、kind、focus（来源消息 ID 字符串）。write_event.kind 为 daily / relationship / project；write_promise.kind 固定为 promise，due 为带时区 ISO 日期时间或 null，初始 status=open、log=[] 由 Ember 生成。focus 在落库前核对来源范围，只存储供原文截断使用，不展示给模型。

key_quotes、objects、participants、context、state、outcome、affect、details 均已删除；理解、情况、细节和感受写入 core。原文读取失败时 briefing 只保留 core。旧测试数据不迁移。

revise_memory_event 可直接改 summary / core / significance / 非 promise 的 kind 及 recall_cues，不再受旧 Sleep 定稿限制；来源绑定不可修改。约定状态与改期由后续的约定处理工具承担。


### 原文增量提供方契约（2026-11-01）

Ember 在 Event 落库后后台拉取，以及 POST /ember/briefing/refresh 时同步兜底；不再需要 App 另行通知切片。turn_end 仅提交生成完成及来源身份，不再启动切片任务。旧 /ember/slices/jobs 仅能查询历史任务。

原文提供方需要实现 GET /api/source/increment?cursor=<JSON 编码的不透明游标>&limit=<页大小>，鉴权与 X-Heart-Instance 同 /api/source/path。初始 cursor=null；返回 {cursor, has_more:boolean, turns:[{session_id, messages:[{message_id,role,content}], user_is_system?:boolean}]}。只列出已完成且由提供方验证历史分支路径的完整人机消息对，或 assistant 单条；系统用户消息由提供方显式标记。游标属于该实例的增量流，不能用当前叶节点或最大消息 ID 猜测历史分支。has_more=true 时游标必须前进；空页同样返回 cursor 与 has_more。

两个触发点共用一个持久游标和排他执行锁，切片以消息身份去重；绑定、筛选和向量写入成功才推进游标，失败保留游标供重试。briefing 返回 source_sync 状态；来源或模型不可达时保留准确的已有记忆，报告 unavailable，不假装增量已同步。刷新接口现会等待原文拉取与嵌入，应设置适当请求超时。App 原文服务适配此接口后再启用新链路。

有上级的切片仅在切片自身与上级 core 分别过门槛后带出符合 kind 规则的上级 Event；不直接呈现原文。无上级且有信息量的切片才直接呈现。阈值、典型废话、页大小等见 config/lifecycle.json（EMBER_LIFECYCLE_CONFIG 可指定替代文件）；所有嵌入仍由 config/models.json 的 embed 用途和统一网关提供。


### 约定处理（2026-11-01）

list_pending_promises 返回真正进过上下文而尚未写 log 的约定及其原文 span、时间、进入路径；process_promise 要求 event_id 和非空 log，可选 status=done/dropped、due=带时区 ISO 日期时间或 null。未曝光约定不能通过该工具修改。写 log 后才确认处理完；状态不变也必须写。追加 log 与曝光回执可在重启后核对恢复。

到期仍 open 的约定追加在 briefing 末尾，不受预算裁切；未来到期或无 due 只按线索浮现，10 分无 due 写入时默认 pin。低分（默认 <=7）完结后 Event 与其独占的切片退出自动召回，主动下钻仍可达。按 config/lifecycle.json 的时区每日零点检查到期满 lapse_days 的 open 约定，写入 lapsed log；启动补算。Windows 需安装 requirements.txt 中的 tzdata。待处理数量只在刷新 briefing 时更新，普通轮次不替换 briefing。


### 共用归组、Entity 与主题概括（2026-11-01）

Event 后台维护使用 config/models.json 的 memory_group / entity_identity / entity_summary / theme_summary 用途。向量只提供候选，LLM 判断一类事或具体对象。低分日常先复制不可变副本；够数后按全部主题副本重概括，用概括替换自动召回里的零散 Event，原件仍可主动检索。低分完结约定池形成同一层的客观主题概括；这层独立于认知/规律。聚合后的 daily 切片仍比原 Event 的 core，命中带出替代主题。

Entity 名称或别名命中只给 summary；Event 命中附所属 Entity 的 summary，卡片去重且不占 Event 三席。Direction 不再参与自动召回。core、cue、切片和主题复用同一 embed 空间及每次输入的同一 query 向量。项目新事实可让旧 Event 退出自动召回，原件和出处不删除。

revise_entity_summary(entity_id,summary) 直接修订卡片并提交变化检查；read_entity_events(entity_id) 下钻具体 Event，再通过原文工具读取原话。Entity 只在新建、别名或 summary 变化时查重；确认同一对象才合并，保留较早卡片、别名、成员及旧 ID 映射。后台失败保留待重试状态，修订期间材料变化则重做，避免覆盖新修改。
