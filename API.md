# ember 接口文档

> ember：旁路记忆系统。职责边界：记忆写入（模型自主+校验）、召回注入、sleep 夜间维护（挂关系/结构维护）、沉淀证据账本；
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

2026-10-20 本段新增 `presentations`（有序呈现粒度）和 `recall_refs`（触发会话、真实锚点 seq 字符串及实际输出事件的出处辅助）。只有事件正文/摘录进入 `recall_refs.events`；实体概览不展开隐藏成员。旧事件或缺可靠消息身份保持 unresolved，路径由 app 解析。完整字段及限制见 [RECALL_REFS_SPEC.md](RECALL_REFS_SPEC.md)。

带 recall_id 的新模式已本地实现持久快照与 24 小时原响应重放，见下节。主动 run 和新原文读取客户端未实现；旧 `/api/source` 调用保持原行为，第二阶段尚未完成真实三方联调或部署。

### 4.1.1 普通生成显式绑定（2026-10-20 本段）

turn_start 可传 recall_id，MCP 随后逐请求带 X-Ember-Recall-Id 和 X-Ember-Tool-Call-Id，turn_end 必须带回同一 recall_id。新调用按活动生成校验，完成工具回执可持久重试；不再借共享当前槽。新增 GET /ember/generations/{recall_id} 及其 /tools/{tool_call_id} 查询。完整字段、错误码、重启与工具读取回执不持久保存规则见 [GENERATION_BINDING_SPEC.md](GENERATION_BINDING_SPEC.md)。

### 4.1.2 召回快照查询及 24 小时正文留存

`GET /ember/recalls/{recall_id}` 默认只返回冻结的呈现/引用元数据；`?view=response` 另带首次完整开轮响应的 response 字段。沿用 Bearer 和实例路由。具体字段和恢复边界见 [RECALL_SNAPSHOT_SPEC.md](RECALL_SNAPSHOT_SPEC.md)。

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
- `key_quotes` 逐字核对，对不上的条目丢弃并进 `dropped_quotes`（记忆本体已保存，不因摘录出入重试）
- 出处（会话/轮次/heart 钉子/message_id）由进行中的一轮自动关联，模型无需提供

成功回执：`{"status": "created", "event_id": "EVT-…", "summary", "raw_ref", "route_count", "recall_cues" [, "dropped_quotes"]}`

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

## 6. Sleep 夜间维护（消费方：app 编排）

用户在 app 侧按键进入 sleep（人为编排，与 heart 入睡档同构）后，app 按固定顺序编排两个阶段：**挂关系**（link，借窗口跑——2026-10-17 起归属判定 extract/attach 与 entity summary 当场写改也全在此处）与**收尾维护**（structure/run，ember 本地+轻活：cue 收紧 + 向量补建 + 观察提炼与合并）。

> **编排顺序（务必对 app 讲清）**：挂关系清完**不等于没活儿了**——之后还有一步 `structure/run`。必须等 link 交卷清零（progress 归零）后再调 structure/run；漏调会漏掉 cue 收紧、向量补建与观察提炼。

```
进入 sleep（app 按键）
  ① GET  /ember/sleep/link/batches     → 材料出队（≤5 批）
  ②    app 把材料贴进窗口末尾，正主 AI 判联（link_relations）
  ③ POST /ember/sleep/link/submit      → 交卷落库（pending 扣减）
       （断线重进：GET /ember/sleep/link/progress 查进度，材料照旧）
  ④ ①-③ 循环到 progress 归零
  ⑤ POST /ember/sleep/structure/run    → 收尾维护（cue 收紧 + 向量补建
       + 观察提炼与合并，见下注）
  ⑥    app 判 dream / 沉淀（走 app 自己的工具与库）
       → app 真的存下一版新沉淀时：POST /ember/sleep/sediment/evidence
```

> **归属与 entity summary 的生产线（10.17 重构）**：归属判定（extract/attach）与 entity summary 的写/改**全部在窗口判联当场完成**——窗口 AI 手里是一手材料，extract 立户当场写第一版描述（3-6 句名片式），attach 当场对照名下全部成员改写。glm 的散事件归属全库扫兜底与夜批 summary 重算**退役**（散事主题延续时召回自举，无需全库扫；verdict 的 entity 段新增 `summary` 字段，宽松校验：未输出/空 → 保旧值）。呈现层随之调整：entity summary 打头，成员事件的 summary 不再单独展示（数据层保留）。structure/run 报告里不再有 `strays_total` / `saved` / `rejected` / `summary` 字段。

> **观察式记忆（10.16 定稿）**：`structure/run` 收尾内置两步——**观察提炼**（每个会话查游标，未被事件 span 覆盖的消息攒够 300 条才调第三方 LLM，产 0-N 条「信息点」：只收行为模式，不收个人档案/具体事件，不硬挖，0 条合法；上限单次 400 条切片）与**高相似合并**（观察向量 ≥0.90 聚组 → LLM 判定并写合并版，旧的标 superseded）。观察事件 `origin=observe`、无 cue、**自动召回走附加席**（每轮拿输入向量比对观察层 core 向量，宽松阈值 ≥0.35 命中至多 1 条，不占 3 席，渲染为「（印象浮现）」弱标注，记账 route=observe）；**判联判出 relation = 印证 → 自动转正**（`origin=affirmed`），判 neighbor/none 放着不动。观察落库即 fill turn_span，判联材料组装与翻原文零改动兼容。配置：`EMBER_OWNER_NAME`（用户称呼，注入提炼 prompt）、`executors.json` 的 `observe` / `observe_merge` 任务点。

> **沉淀分工（10.15 对齐）**：`dream_submit` / `sediment_submit` 都是 **app 的工具**，正文与版本号存 app 自己的库；ember 只收证据 id 追加（6.5）。**证据为空时 app 不调 6.5，什么信号都不发**；「sleep 结束」信号也由 app 自己记，ember / heart 均无感知。

### 6.1 GET /ember/sleep/link/batches

材料出队。pending 里的新事件逐份组装材料（turn_span 区间 ∩ 召回带出的旧事件；superseded 与判过留痕的候选剔除），均分为 ≤5 批。

响应 200：

```json
{
  "batches": [
    {"batch_index": 0,
     "materials": [
       {"new_id": "EVT-…",
        "new_event": {"event_id": "EVT-…", "core": "…", "summary": "…"},
        "entity_name": "",
        "old_events": [
          {"event_id": "EVT-…", "entity_name": "…", "entity_id": "ent_…",
           "entity_summary": "…", "core": "…", "summary": "…"}
        ]}
    ]}
  ],
  "pending_total": 3,
  "materials_total": 3
}
```

- `old_events` 的实体标注（entity_name/entity_id）是 attach 校验的依据，`entity_summary` 是该实体当前的名片描述（attach 当场改写 summary 的底稿），判联材料必读
- **出队幂等**：不扣 pending——只有交卷成功才扣；断线重进材料照旧
- 无活儿：`batches: []` 且 `materials_total: 0`（诚实返回，不凑数）

### 6.2 POST /ember/sleep/link/submit

交卷。app 把窗口 AI 的 link_relations 结果按材料原样转交。

请求体：

```json
{"source_message_id": 123,
 "results": [
   {"material": {"…出队时的材料，原样回传…"},
    "verdict": {"pairs": [...], "entity": {...}}}
 ]}
```

- `verdict` 契约 = 窗口判联 prompt 的输出格式（pairs 逐条 + entity 三分支 extract/attach/none）
- 校验基于**回传材料**：pair 指向材料外事件 → 整份拒绝（防幻觉）；attach 目标必须是材料内旧事件标注的实体
- `source_message_id`（可选）：触发判联的窗口轮，落库留痕 `sleep-link:window:m<id>`

响应 200：

```json
{"saved": [{"new_id": "EVT-…", "relations": ["EVT-A--related_to-->EVT-B"],
            "resolves": [], "entity_action": "attach:ent_…"}],
 "rejected": [{"new_id": "EVT-…", "errors": ["pair 指向材料外事件: …"]}],
 "remaining_pending": ["EVT-…"]}
```

- 落库三分支：relation 落边（resolves 额外标 superseded + change 补写）；extract 开新户挂散事件；attach 单归属挂入 + 顺带判目标实体收尾
- 整条拒绝不炸整批：单份材料校验失败进 `rejected`，其余照常落库

### 6.3 GET /ember/sleep/link/progress

进度/断点续跑。响应 200：

```json
{"pending_total": 2, "materials_ready": 2,
 "batches_remaining": 2, "pending_events": ["EVT-…", "EVT-…"]}
```

断线恢复：按 `pending_events` 从 6.1 重新出队即可，判过的 pair 有留痕不会重复落库。

### 6.4 POST /ember/sleep/structure/run

收尾维护（无请求体，幂等）。**2026-10-17 重构后四步全在 ember 本地**：向量补建（新事件/新实体的 core 向量）→ cue 收紧（闲置旧 cue）→ 观察提炼（逐会话查游标，未覆盖消息攒够门槛才调第三方 LLM，产 0-N 条观察）→ 观察合并（高相似观察聚类判重写）。归属判定（extract/attach）**不在本步**——已收编窗口判联（见 §6 开头注记）。

> **线程模型（2026-10-19）**：这步是几十秒的重活，ember 在工作线程上跑——**其他实例的钩子请求照常被接待，不再排队**。但本请求本身仍等到跑完才返回（sleep 是顺序编排，sediment 依赖本步完成——等待是契约，排队不是）。失败如实回执 5xx，不静默重试——重试决定权在 app（幂等，直接重试安全）。

响应 200：

```json
{"vector_backfill": {"events": 3, "entities": 1, "failed": 0},
 "cue_tighten": {"checked_events": 10, "retired": [], "skipped": [],
                  "kept": [], "kept_zero_use_event": []},
 "observe": {"status": "ok", "created": 2,
              "sessions": [{"session": "main", "status": "ok",
                             "uncovered": 320, "processed": 300,
                             "created": 2, "event_ids": ["EVT-…"]}]},
 "observe_merge": {"candidates": 4, "groups": 1, "merged": 1}}
```

- `observe.status`：`ok` / `unconfigured`（未配 `EMBER_SOURCE_URL`，此时无 `sessions` 字段）；单会话失败不牵连整批（session 级 `failed` / `unavailable` 如实上报）
- 观察提炼的第三方 LLM 调用/解析失败会 5xx，直接重试即可（幂等：已提炼会话的游标已推进，不重复产观察）

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

### 7.1 原文提供接口（`/api/source`，Deliz 将来照此 schema 接管）

10-14 去原文化后，ember 本地不存会话原文，原文只存后端消息树一份；ember 按需拉取、**用完即弃、本地不落盘**。当前提供方 = heart-chat 只读端点；Deliz 上线后只换 `EMBER_SOURCE_URL`，ember 与调用方零改动。

请求（GET，`EMBER_SOURCE_TOKEN` 设置时带 `Authorization: Bearer …`）：

```
GET {EMBER_SOURCE_URL}/api/source?session_id={sid}&from={int}&to={int}
```

- `from` / `to`：**message_id 整数区间（含两端）**——对外契约只有 message_id 一套（见 §9）

响应 200：

```json
{"messages": [
  {"message_id": 123, "role": "user", "content": "原文整行，不截断"},
  {"message_id": 124, "role": "assistant", "content": "…"}
]}
```

- 按 `message_id` 升序；区间内没有消息 = 空 `messages` 数组（合法，不是错误）
- `content` 原样返回，**含被前端截断展示的完整文本**——ember 要做引文逐字核对

失败语义（ember 侧的消费方各自降级，提供方只需如实）：

| 情形 | ember 行为 |
|---|---|
| 未配置 `EMBER_SOURCE_URL` / 网络失败 / 非 200 / 响应缺 `messages` | 按「语料不可用」降级：写记忆引文核对 → 拒绝写入；read_memory_source → `not_archived`；sleep 材料 → 跳过该项 |
| 区间拉不到对应消息 | 返回空数组即可，不报错 |

- **绝不重试轰炸**：一次拉取失败就降级，下次需要时再试
- 提供方鉴权与 heart-chat 现行一致（Bearer，可选）

## 8. 简报（briefing，消费方：app 截断编排）

**2026-10-17 设计夜定稿。截断驱动、拼装式零 LLM**：上下文丢头留尾式截断，每次截断时 app 调 refresh 刷新简报并注入留下上下文的开头；旧简报随头部丢弃，新简报自包含。断线重进/新会话用 GET 取。

- `POST /ember/briefing/refresh`：体 `{"session_id", "keep_from_message_id"}`（现场起点的后端消息行 id——现场区间记忆不进，原文已在上下文；ember 经账本换算，app 不接触内部轮号）。返回 `{"status": "ok", "text", "chars", "stats": {…}}`，`text` 即注入文本
- `GET /ember/briefing`：取当前简报；从未刷新 → `{"status": "empty"}`
- 入选/分层/预算/去重全规则见 **BRIEFING_SPEC.md**（要点：pin 与 significance=10 必进给原文层；72h 新增 8-9 给 core、1-7 给 summary 一句；召回过的旧记忆只放最相关一条；现场与观察事件不进；预算 6000 字从低层砍，pin/10 永不砍；简报注入计一次在场——2h 内召回走轻量行）。每条条目带绝对时间标注 `[MM-DD]`（跨年 `[YYYY-MM-DD]`，取经历时间 event_time；缺失则无标注）
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
| EMBER_SOURCE_URL / EMBER_SOURCE_TOKEN / EMBER_SOURCE_TIMEOUT | 原文提供接口（heart-chat `/api/source`，将来 Deliz 同 schema 接管——契约见 §7.1） |
| EMBER_EMBED_BASE_URL / EMBER_EMBED_API_KEY / EMBER_EMBED_MODEL | 向量服务 |
| ZHIPU_API_KEY / EMBER_SLEEP_MODEL | 观察提炼/合并等任务的 LLM |
| EMBER_OWNER_NAME | 观察提炼注入 prompt 的用户称呼（不配写 "user"，见 §6 观察式记忆） |
| EMBER_BRIEFING_BUDGET | 简报字符硬顶（缺省 6000，见 §8） |
