# 协议细节

HTTP入口与通用字段见[API.md](API.md)。本页集中保存briefing、引用、快照、主动run及来源完成的技术细节，按稳定锚点定位。当前记忆维护链路见[MEMORY_LIFECYCLE_SPEC.md](MEMORY_LIFECYCLE_SPEC.md)。


<a id="briefing"></a>

## Briefing

> 交付对象：Deliz 侧（app 编排）。ember 侧实现已完成，本文是契约全文。
> 简报 = 拼装式（ember 查表组装，**零 LLM、零 token 成本**），截断驱动。

### 1. 它是什么、什么时候发生

- 用户上下文采用**丢头留尾式截断**：截断时头部丢弃、尾部（最近一天左右的对话）留在上下文。
- 旧简报文本在头部——**随截断一起丢掉**。所以每次截断时生成的新简报必须**自包含**（重新带上所有还活着的认知），app 拿到后注入到留下的上下文开头。
- 断线重进/新会话开场：app 随时可用 GET 接口取当前简报重新注入。

### 2. 接口

#### 2.1 POST /ember/briefing/refresh（截断时调用）

请求体：

```json
{"session_id": "main", "keep_from_message_id": 1024}
```

- `session_id`：必填。
- `keep_from_message_id`：**现场起点的后端消息行 id**——截断后留在上下文里的最早那条消息的 seq。现场区间的记忆一律不进简报（原文就在上下文里，带了就是复读）。ember 经 turn_ledger 换算成起点轮（app 不接触内部 turn 坐标，与 API.md §9「对外只有 message_id 一套」一致）；换算不到（该消息没进过账本）→ 缺省同 0 = 不做现场排除（保守多带）。
  - 现场判定口径（2026-10-17 修）：只看**本会话**的事件，且以**经历区间**（turn_spans，内容真实所在轮次）是否伸进保留窗口为准——记忆可以在很新的轮次里写下很旧的事，出生轮次（turn_refs）不作数（turn_span.py 2026-10-12 裁决：出生时刻不冒充经历时刻）；区间未回填 = 未知出处，不算现场。

响应 200（`text` 就是注入文本，直接贴上下文开头）：

```json
{"status": "ok",
 "text": "◆ [10-15] ……\n• [10-17] ……",
 "chars": 2381, "session_id": "main",
 "stats": {"pinned": 1, "sig10": 1, "new72": 8, "recalled_top": "EVT-014-ab12cd",
            "dropped": {"core": 0, "summary": 2}}}
```

- `text` 是**纯条目列表，无标题行**（2026-10-18 momo 拍板）——标题（如「近况」）由 app 注入时自己加，ember 不替 app 定文案。
- 每条条目明确标注 `[日期：YYYY-MM-DD]`，同年也保留年份。`event_time` 缺失、null 或空白时显示 `[时间缺失]`；存在但不可解析时显示 `[时间解析失败]`，不使用写入时间兜底、不猜日期（2026-10-24）。该日期来自存储的 `event_time`；普通写记忆目前将其设为写入时刻，不保证补记历史事件的实际发生日期。已有缓存由下一次正常 refresh 更新，不因读取而重新选材或记账。

#### 2.2 GET /ember/briefing（取当前简报）

返回最近一次 refresh 的结果原样；从未刷新过 → `{"status": "empty"}`。

#### 2.3 钉住

- 工具：前端 AI 的 `pin_memory` / `unpin_memory`（用户明确说「钉住」时调）。
- HTTP：`POST /ember/memory/pin`、`POST /ember/memory/unpin`，体 `{"event_id": "..."}`，幂等。
- 联动：钉住的**必进简报（原文层）**且**自动召回不再浮现**（search 主动检索不受影响）。

### 3. 入选与分层（ember 拼装规则，app 无需关心，列出供理解）

| 类 | 入选 | 层次 |
|---|---|---|
| pin + significance 10 | 必进（现场除外） | **原文**：core + ±3 轮原文（事件是真区间则按区间；单句扩 ±3 轮，不足有多少拉多少；原文不可达退 key_quotes） |
| 72h 新增（现场外） | 都要 | significance 8-9 → **core**；1-7 → **summary 一句** |
| 召回过的旧记忆 | **只放最相关一条**（被召回次数最多，平局取最近；简报在场不计入召回次数） | core |
| 现场尾部（本会话、经历区间 ≥ 起点轮） | **不进** | —— |
| 观察事件（origin=observe） | **不进** | ——（习惯不是「正在进行」） |

- 层次依据 = 写入时前端 AI 标的 `significance`（**2026-10-17 起格式为 `"N：一句话理由"`，N∈1-10**，写入端硬校验）。旧事件无数字前缀按 5 计。
- 预算 **6000 字**（`EMBER_BRIEFING_BUDGET` 可调）：超了从低往高砍——先砍 summary 行，再按 significance 从低砍 core 行；**pin 和 10 分永不砍**。`stats.dropped` 如实报砍了几条。
- 去重：同一事件在一份简报里只出现一次（类间取最高层）。

### 4. 与召回的去重（ember 内部已实现，app 无需动作）

- 简报注入 = 一次「在场」：简报带过的事件记入在场账（ledger route=briefing），**之后 2h 内被召回时只走轻量行报到**，不重复展开。
- 钉住的事件：永久不自动浮现（既有裁决），简报是它们唯一的常驻出口。

### 5. app 侧要做的

1. 截断动作处：取留尾的起始消息 seq（`keep_from_message_id`）→ 调 `POST /ember/briefing/refresh` → 把 `text` 注入留下上下文的开头（标注来源「近况简报」即可）。
2. 新会话/断线重进：调 `GET /ember/briefing`，非 empty 就注入。
3. 用户在 UI 钉/取消钉记忆（如果做 UI 的话）：调 pin/unpin 端点。
4. 前端 AI 的写入行为有变：`significance` 必须是 `"N：理由"` 格式（写入端会拒），participants 别漏 ai。


<a id="recall-refs"></a>

## 召回呈现引用

2026-10-20。对齐 Heart 三方契约 v0.6 中可独立实施的呈现元数据/出处辅助部分。仅本地实现与隔离测试，不等于完整第二阶段或生产部署完成。

### 1. 已实现与明确未实现

已实现：现有 `/ember/hooks/turn_start` 响应新增 `presentations` 与 `recall_refs`；新实时事件写入时保存独立的 `event_sources.jsonl` 来源账。

原有 injected_text、memory 统计、门槛、席位、轻量续注、选材和 `/api/source` 消费路径保持原行为。新字段不参与召回筛选，缺映射不使聊天失败。没有接入 Heart 推送、ODE、学习或日常调度。

后续已实现普通生成绑定和稳定 recall_id 快照、in_progress/查询/恢复及用户确认的 24 小时正文 TTL，见 GENERATION_BINDING_SPEC.md 和 RECALL_SNAPSHOT_SPEC.md。不带 recall_id 的旧钩子重试仍会开新 turn。主动 run/pending 和新原文读取客户端未实现，未改 app 或 Heart 仓库，尚未切换 `/api/source/path` 或完成三方联调。

### 2. 轮前响应的新增字段

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

### 3. 来源账：固定已知身份，不猜缺失的那半轮

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

### 4. 后续交接与验证边界

app 原文解析/读取的新实现保持现状，旧 `/api/source` 暂不改。待 Ember 的新路径读取客户端完成并覆盖翻原文、写入核对、sleep 等消费方后，再协调迁移，不提前收紧旧接口。

普通生成显式工具绑定和稳定召回快照已本地实现，见 GENERATION_BINDING_SPEC.md 与 RECALL_SNAPSHOT_SPEC.md。后续独立工作仍包括 run/tool 专用证据、pending 查询和完成、原文客户端迁移及三方联调。pending 仅针对主动轮中 AI 已显式调用的记忆工具；没有调用就不生成 pending 或长期 Event。整个唤醒链路不训练 Heart 耳朵。

隔离测试覆盖：正文/摘录/概览区分、隐藏和落选成员排除、轻量/修正/观察呈现、原文字串兼容、实际轮前响应、来源冻结、重启与最大 reply 不改来源、无编号和旧语料、跨会话/实例、缺版本、无内容事件及引用失败无半成品。

本次验证结果：136 项 unittest 通过；独立验收脚本全部成功退出（两项真实数据验收缺数据跳过）；19 个旧版/新版渲染对比场景逐字一致。未做真实三方联调或生产部署。


<a id="recall-snapshots"></a>

## 召回快照

2026-10-21 更新：同生成来源补交及 sources_revision 推进已实现，见 SOURCE_COMPLETION_SPEC.md。原 snapshot/response 冻结；source_candidates 独立持久更新，实际变化时修订 +1，新召回初始为 0。下文首版固定 0 的描述仅对应历史 156f29e；运行版本见隔离就绪回执。

2026-10-20。本地已实现，未提交、推送、部署或做真实三方联调。用户已确认临时召回正文保留 **24 小时**。本文取代此前文档中“快照未实现、TTL 待确认”的阶段说明；主动 run 仍未实现。

### 1. 开轮和重试

沿用 `POST /ember/hooks/turn_start`，提供真实 `session_id`、`message`、`user_message_id` 和稳定 `recall_id`。普通工具头和收轮要求见 GENERATION_BINDING_SPEC.md（本地历史记录）。不带 recall_id 的旧模式不获得快照幂等能力。

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

### 2. 查询接口

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

### 3. 正文保留规则

- 从完整响应首次持久提交开始计时，固定 86,400 秒，不从用户消息时间计算，读取和重试不续期。
- 到期清空本实例 generations.sqlite3 的 response_json，包括临时 injected_text/统计响应副本；原有 Event、app 聊天、工具回执不随这项 TTL 删除。
- 会话/轮身份、业务哈希、呈现分类和引用元数据继续保留，用于防止旧 ID 复用及迟到解析。不是删除整份记忆，也没有复制 app 全部原文。元数据的后续清理与会话删除通知尚无接口。
- 查询严格检查到期；运行中的 sidecar 启动即清理，之后每 60 秒检查已配置实例，无请求时也清理。实例服务加载时亦清理。服务停机期间无法执行清理，恢复运行后补清。
- SQLite 清理连接启用 secure_delete，避免仅将字段置空却留下可直接从数据库空闲页读出的旧副本；不承诺清除外部备份、操作系统缓存或磁盘历史。
- read_memory_source 的工具读取回执仍只在进程内保存原文，不因本规则改为持久保存。24 小时规则只用于轮前召回响应快照。

### 4. 持久化与恢复边界

同实例由一个服务进程拥有。先在 SQLite 原子占位身份和预留 turn，再计算召回；同 ID 只有一个创建者，其余返回已固定结果或 pending。预留 turn 在中断后也不复用。

最终响应、引用元数据、生成状态和待应用记账操作在同一 SQLite 事务提交。之后将带稳定 operation_id 的轮前映射和召回统计写入 JSONL，flush/fsync 后确认；启动/查询/下一次开轮可重放未确认操作。JSONL 索引按 operation_id 防重，保留第一次的时间，修复截断尾行后继续追加。不把旧 hook 整体先跑完再写缓存。

未提交结果前中断，重启后保留 unknown 身份而不重新选材；提交后记账中断，恢复原操作并返回原快照。恢复不重新打开旧生成。记忆工具的 Event JSONL/工具回执仍遵守原有不确定结果纪律，不能据快照事务宣称所有工具跨存储 exactly-once。

### 5. app / Heart 下一步

2026-10-21 更新：App d62aad1 已报告本地接入 turnEnd.recall_id、逐请求普通 MCP 两个身份头及恢复分支，假服务验证通过，尚未真实联调。迟到 ready 的原轮次 turn_end 需要补发，持久时机、状态和幂等规则见 [TURN_END_RECOVERY_SPEC.md](PROTOCOL_DETAILS.md#turn-end-recovery)；正文到期但 refs ready 时仍可补。由 app 唯一驱动迟到解析；原始 Heart revision=0 登记持久确认后，解析变化才另发 resolution 操作，原始包不改。Ember 不直推 Heart、不定时替 app 补解析。

Heart 可查看本文及 APP_交接回复.md（本地历史记录）；快照补取不新增用户刺激、不复活旧唤醒周期。原文读取 `/api/source/path` 客户端迁移、独立主动 run、工具结果证据与显式 pending 核验仍未完成。临时提醒不入档、不自动召回；整个唤醒链路不训练耳朵，不新增每次唤醒自动写记忆。

验证：191 项 unittest 通过，含快照重试、实例/鉴权隔离、真实 ASGI pending、精确 24 小时到期、数据库正文清除、并发单创建者、提交前后故障与 JSONL 恢复。真实三方联调尚未执行。


<a id="runs"></a>

## 主动run

2026-10-24。用于 App `wake-integration` worktree 接入；本次只在 Ember 工作区实现及隔离测试，不部署日常、不修改 8790、不启用主动发送。普通对话的 recall_id/turn_start/turn_end 保持独立。

### 通用要求

沿用 Ember Bearer 认证和逐请求 `X-Heart-Instance`，每个实例一份 `runs.sqlite3`，不存在跨实例回退。运行身份由 App 传递，不进模型提示、不用普通 `_live_turn`，不造 user/assistant 消息、turn、自动召回或长期 Event。

POST 均是严格 JSON 对象；必填键即使值为 null 也须给出。未声明字段（包括提醒/正文的 start 字段、伪 branch_id）拒绝。标识符为非空、不带首尾空白、最长 512 字符的字符串。消息号为正整数或十进制字符串，拒绝布尔值与重复编号；数组顺序照录，不按 seq 推测路径。

### POST /ember/runs/start

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

### POST /ember/runs/finish

必填：`event_id, kind, run_id, wake_id, session_id, status, spoke, assistant_message_id, assistant_reply, tools, finished_at`。status 仅 done/cancelled/error，finished_at 固定、带时区且不早于 occurred_at。

- spoke=true：仅 done；assistant_message_id 是已提交的真实 assistant seq，assistant_reply 是非空实际正文。必须与原文提供方的精确路径、角色和正文一致，不能引用 start 窗口已有消息号。
- spoke=false：assistant_message_id 必须显式 null、assistant_reply 必须空串；完全安静或只有工具行动也可 done，取消/失败不造可见回复。
- tools：最多 1000 个 `{tool_call_id,name,status,result_ref}`。status 为 completed/failed/unknown，tool_call_id 唯一。result_ref 固定 `wake-tool:<run_id>:<tool_call_id>`。

工具引用仅指 App 的本地账，返回 `tool_evidence=app_metadata_only`；不访问该指针，不把它当已验证正文或长期 Event 证据，也不覆盖 Ember 自己的 MCP 工具回执。

先持久固定 finish 意图、冻结新工具调用（state=finishing），再校验可见回复。临时原文不可用返回 503 run_source_unavailable + Retry-After:2，保留该意图，App 继续投相同 event_id、正文、工具和时间。进程重启后也只能续验该意图；不能用另一终态或另一 body 替换它。真正路径/角色/正文不匹配返回 409 run_source_mismatch，留待对账，不谎报 done。

成功返回 200、同一 run/wake/session、原终态、event_id、spoke、assistant_message_id、finished_at 和工具元数据。首个终态不可变；相同操作重放原结果，不重读原文或重执行工具，新操作指向终态返回 409 run_finished。存储 finish 请求哈希和无正文元数据，不落 assistant_reply。

### GET /ember/runs/{run_id}

返回 200，身份与 `status=running/finishing/done/cancelled/error`、开始版本/路径、`execution_context_available`、`finish_event_id`、`tool_calls`（调用身份、名称、执行状态和结果状态/code，无正文）。终态附 spoke、真实回复 seq/工具元数据与 finished_at；运行中的 spoke 未知，不填 false 冒充安静结果。

只有确实无该实例记录才 404 `run_not_found`。finishing 是待固定完成意图核验，不是失败/取消。重启保留 running/finishing/终态，不自动改成 error，也不恢复执行权：GET 的 execution_context_available=false 时不得重跑模型/工具；App 用其本地持久结果补投 finish，未知则保留对账槽。

### MCP 请求绑定及首版权限

每次工具调用带 `X-Ember-Run-Id` 和稳定 `X-Ember-Tool-Call-Id`。不可混用 `X-Ember-Recall-Id`，缺/空/未知/已结束或上下文丢失均拒绝，不能回退到普通对话。工具清单沿用实例通用清单，App 必须与 start 返回 allowed_tools 取交集；服务器也会拒绝其余工具。

首版只准 `read_memory_source`。**search_memory 不是纯读**：现有实现会记提取账、刷新遗忘时钟和恢复 Cue，所以首版返回 `run_tool_not_allowed`。App 当前 WAKE_READ_TOOLS 含该项，需要移除或按 allowed_tools 过滤；不要求改普通对话搜索语义。

调用身份先持久登记再执行；同 ID 改内容 409，未知结果不重执行。读结果仅暂存在进程内，最多保留 256 条；缓存淘汰/重启后同身份返回 result_not_retained，不悄悄重读。未完成结果返回 tool_outcome_unknown。已存在的完成回执可重放，终态之后的新调用拒绝。实际读出的正文、异常原文和工具结果不写入 SQLite。

### HTTP 拒错摘要

| HTTP | code / 含义 |
|---|---|
| 400 | invalid_arguments / invalid_identity，含具体 field（如有） |
| 401 | Bearer 认证失败 |
| 404 | 实例未知；或 run_not_found（确无记录） |
| 409 | id_conflict / context_mismatch / run_busy / run_finished / run_source_mismatch |
| 503 | run_source_unavailable，继续投同一固定 finish |

MCP 失败仍为正常 RPC 响应中的 `isError=true` 与业务 code；run_context_lost、run_tool_not_allowed、tool_outcome_unknown、result_not_retained 必须保留各自语义。

### 明确未开放

主动 create/revise/pin/unpin、专用工具结果长期证据、主动 Event 原文协议和 pending 写入均未开放。无显式调用不建待写记忆；本版 finish 不创建 Event，不向 Heart 发 echo，不训练耳朵、不改变调度或日常开关。完整真实三方联调、断线/重启组合以及日常部署需在 App 接收上述字段/工具范围后进行。

### 验收结果

最终实现的 VPS 临时目录完整回归 293/293 通过，旧 sidecar 38/38。新增主动 run 专项 24/24（包含 4 项 HTTP/MCP 和 2 个真实进程崩溃检查），资源警告按错误模式也通过；可见回复按提供方的实际返回顺序核对，不用编号集合代替顺序。App 既有真实进程隔离 wake-http-check 20/20；导出的 3 个真实 start 请求直接通过 Ember 校验。它们不等于真实 App + Heart + Ember 三方已联调，更不代表日常主动发送已开启。


<a id="source-completion"></a>

## 来源补交

2026-10-21 验收补充：App a7d82f3 / §11 已报告真实 resolved、迟到 resolution 与三方幂等通过；本方逐项核对及仍缺的故障场景见 SOURCE_COMPLETION_COVERAGE.md。完整历史轮上的 source_recall_id 只是身份标记，不要求每轮都有补交。下文“App 仍需补验”为交付时点记录，最新状态以本段和覆盖表为准。

2026-10-21。已读 `D:/deliz/APP_第二阶段回复.md` §9，按用户确认实现同生成 done 后的独立来源绑定、source_candidates 与 sources_revision 更新。本文为已实现契约；交付代码及隔离服务加载版本见本次提交和 D:/phase2-secrets/ember-readiness.json。App 仍需接纳新证据并补验真实出处解析及 Heart resolution，不能把 Ember 自测当成这两项已经通过。

### 1. 为什么能补，补的是什么

记忆工具在最终回复提交前执行，来源语料可能只有该轮真实用户消息。此时缺 assistant seq 是如实冻结的状态。后续 done 收轮能证明原生成实际提交了哪条回复，因而可以补齐该来源轮的闭合路径。

这份新证据只证明“这个来源轮后来由这条真实回复完成”，**不证明该回复文本在写记忆时已经存在、被 quote_spans 引用或参与了原核验**。原 key_quotes/quote_spans 的逐字核验不变，不给原本不存在的引用补证，不新增 Event、不重写其内容或来源跨度。

不能只拿 writer_recall_id 填整个事件的所有来源轮。多轮事件只补已绑定到该生成的那一轮；其他轮缺证据仍 unresolved。以后可支持 user-only 的精确引用，但不在本方案里改变 App 现有按轮取闭合路径的规则。

### 2. Ember 负责核验和持久补交

适用条件必须全部满足：

1. 原记忆确由已登记普通生成的显式 create_memory_event 创建；有 writer_recall_id、writer_tool_call_id、已完成且返回该 event_id 的工具回执，以及不可变原出处记录。
2. 待补的具体来源轮属于同一实例、同一 source_session_id，原出处跨度已包含该轮，user seq 已在写入时核验。来源轮必须精确匹配该生成的原 turn、真实用户 anchor；不得只看“创建发生在这一轮”。
3. 新写入的出处 schema_version=2，在 pending_sources 中另记该缺失位置的 turn、user_message_id、pending_source_recall_id，只标记确属写入生成且无已存在 assistant 语料的缺失位置；不靠当前 live 槽或以后变大的 reply 倒推。writer 身份与逐轮 source 身份继续分开。
4. 收到同 recall_id、同 session/turn 的 done，含真实非空最终正文和真实 assistant seq，沿用现有收轮业务哈希/冲突校验。只在这些条件成立后，把真实 seq 记入独立的 completion binding。
5. 已有确切 assistant seq 不覆盖；重复相同绑定不产生新证据或修订。已有终态或编号冲突须对账，不以新编号替换。error/cancelled、unknown、无终态或身份不明不补。

补交记录绑定原出处记录的稳定标识/哈希及原内容身份，而非查询时的当前 Event。原 event_sources 首条记录保留；独立旁路账只保存身份、seq、证据类型与操作标识，不另存完整回复正文。即使 Event 后来 revise，也不能从最新内容重建旧召回的出处。

对于现有 156f29e 创建的部分出处，可以在原工具完成回执、原生成/用户锚点、原冻结来源轮与已完成 done 均可逐项核对时建立显式兼容补交；缺任一证明则保留 unresolved。不对 legacy 无身份写入、历史最大 reply 或不可信语料做批量回填。App §9 的具体测试事件须按这些条件核对，不预先宣称全部可修复。

### 3. 快照与来源修订

- 原 snapshot、recall_refs、presentations、首次完整 response 和 App/Heart revision=0 outbox 永远不改；同 ID start 仍返回原响应。
- 某 recall 的冻结事件列表已含该事件，且补交能与该条目原内容/出处身份严格对应时，仅更新独立 source_candidates 视图。事件顺序、event_id、version_id、presentation、原 session/spans 及其他已知 seq 不变，只补该缺失位置和明确证据说明；不新增隐藏成员。
- 该 recall 的 sources_revision 在可用候选实际变化时持久 +1。一次有效更新只推进一次；同 done 重试、查询、重启恢复不推进。同一证据影响多个既有 recall 时，各自维护修订，不共享计数。
- 一次 done 补交与受影响视图的更新需要事务或可恢复操作账。崩溃后从同一证据继续，不发布“新修订配旧候选”的半成品；未完成的工作可幂等恢复，不能只在内存回调里补。
- 补交在某次新召回创建前已完成：新 snapshot 可以冻结当时已有的完成绑定，初始 sources_revision 为 0。不能给一个尚未创建的 recall 虚构历史修订。
- 24 小时正文 TTL 不变；正文过期但 refs 保留时仍可提供来源修订，不重新保存或延长 injected_text，不刷新召回/提取时钟。

因此，**更新的是“实际包含该事件的召回”，不一定是“写记忆那次生成的 recall_id”**。写入发生在其开轮快照之后，该快照通常根本不含这个新事件；不能为了补交把新事件塞进写入轮的旧快照。

### 4. 给 App 的证据形状

原本在写入时已具备完整消息身份的条目继续使用 source_evidence=frozen_messages。经过本次独立补交的条目使用新增类型 **generation_completed**，不把后来证据重新命名成写入时冻结的原文。

source_candidates 中单个条目的补交字段示意（其余原字段保持）：

```json
{
  "source_evidence": "generation_completed",
  "turn_messages": [{
    "turn": 7,
    "user_message_id": "101",
    "assistant_message_id": "102",
    "source_recall_id": "r-install-original"
  }],
  "completion_bindings": [{
    "turn": 7,
    "recall_id": "r-install-original",
    "user_message_id": "101",
    "assistant_message_id": "102",
    "evidence": "turn_end",
    "status": "done"
  }],
  "resolution": "unresolved",
  "reason": "source_path_unknown"
}
```

App 必须给 RefEvent 增加相应类型/字段支持。接纳 generation_completed 时核对补交与来源轮的身份/编号一致，再按真实消息库、原会话与父链验证；不能只加一个白名单字符串就直接判 resolved。Ember 仍不替 App 证明原文树路径。中间轮缺所需编号、未绑定的来源、跨会话/兄弟分支等继续 unresolved。混合多轮条目中，未补部分仍依赖原 frozen evidence，不因一项补交而全部升级可信。

turn_messages、source_evidence、completion_bindings 仍仅限 Ember→App 核验，不透传 Heart。Heart 继续收现有白名单，不需要新增接口。

### 5. App 负责迟到解析与 Heart 修订

App 继续是唯一触发方：查询发现 sources_revision 增加，核验 source_candidates，调用自己的解析函数。只有解析结果实际变化且原初始登记已被 Heart 持久接受，才发新操作身份的 resolution；不修改初始包、不新增成员/版本。

sources_revision 与 Heart resolution_revision 各自独立：新证据仍不能解析时，不为形式增加 Heart 修订；原文服务修好而 Ember 修订未变时，App 也可用已有可信候选重试。旧引用包初始为 unresolved，不能因补交而悄悄改写同一幂等请求。

Ember 不直推 Heart，不重新召回/注入、不新增刺激、不复活周期、不训练耳朵、不自动写新记忆。旧 /api/source/path 客户端迁移、原文提供方未配置、会话删除通知仍是单列事项，不因本方案而宣称解决。

### 6. 下一段验收

Ember 已实现独立补交与可恢复修订；App 新证据核验仍由 App 接线。隔离验收覆盖：

1. 工具写入→同生成 done→后续新召回：可用真实消息树解析。
2. 写入→尚未 done 时被另一轮召回→done：原 snapshot 不变，候选补齐、sources_revision +1，App 独立 resolution 成功。
3. 同 done 重试、补交中崩溃/重启恢复：无重复推进，不重跑工具。
4. 已替代生成的历史 done、正文过期后补交：不影响当前生成，仍可更新匹配的来源辅助。
5. cancelled/error、跨实例/会话/生成、历史不可信编号、多轮缺证据、改变过内容/来源身份：不冒认来源，保持 unresolved 或冲突。

新增 source_completions 与 recall_sources 两个 SQLite 旁路表。完成绑定按原出处哈希和来源 turn 唯一；绑定与受影响候选/修订在同一事务发布。原工具完成回执、原开轮元数据及不可变 done 回执共同充当持久恢复依据，不另存回复全文。启动、查询、开轮及重复收轮会恢复；done 已提交而补交事务暂时失败时，done ACK 仍有效，refs 查询在恢复失败期间返回 202 recall_recovery_pending，不伪报已更新。

213 项 unittest 通过，其中新增 22 项覆盖精确绑定、旧版兼容、错误身份、工具回执未知、事件身份不匹配、多轮缺证据、终态冲突、过期、事务回滚和重启恢复。真实 HTTP/MCP 探针为 scripts/check_source_completion_http.py；现场结果与加载版本写入隔离就绪回执。App §9 通过项仍按其原报告记录，本段不重跑 App/Heart，不新增 backfill 协议，不启用生产开关或唤醒调度。


<a id="turn-end-recovery"></a>

## 轮后完成与恢复

2026-10-21。已读 `D:/deliz/APP_第二阶段回复.md` §6，核对 App 提交 d62aad1 的查询/收轮客户端与 Ember 当前实现。**需要补发原轮次 turn_end。** 本文澄清现有接口，不新增业务接口、不修改 App 代码，不代表真实三方联调或生产验收。

### 1. 何时补发

补发需要同时满足两个条件：

1. Ember 原生成已可靠登记：首次成功 start 已得到并持久保存原 turn，或 `GET /ember/recalls/{recall_id}` 默认 refs 查询返回 200、status=ready，且核对实例、原 session_id、recall_id、真实用户 anchor 和正整数 turn 一致。
2. App 本地已持久提交该生成的真实终态及所需证据。done 必须有属于原生成的已提交非空最终回复和真实 assistant seq；error/cancelled 必须有对应实际终态，不拿停机或超时自动猜终态。

两者哪个后到，就在其持久确认后把收轮任务置为可投递：

- 聊天先结束、召回后查到 ready：用原终态补收轮。
- 召回先恢复、聊天仍在生成：只登记原 turn 并等待真实终态，不提前结束。
- App 重启：从原生成/消息和持久收轮意图恢复，符合两项条件再投递，不重跑模型或工具。
- ready 但 events=[] 或没有合格事件引用：仍需收轮；收轮不依赖是否有内容可转 Heart。

建议在原生成终态与真实回复提交的同一 App 本地事务中登记独立收轮意图，键为 `(instance, recall_id, ordinary_turn_end)`。原 turn 尚未知时保留 waiting_registration；恢复原 turn 后固定请求并独立重试。只靠一次内存回调不足以跨 App 重启补齐。

收轮与 Heart 初次召回登记/resolution 独立，不互相等待或借用 outbox 确认。快照内容的迟到解析可以继续；补收轮不使旧快照的 unresolved 变成已证明来源。

### 2. 身份与请求

沿用 `POST /ember/hooks/turn_end`、原 Bearer 和原 `X-Heart-Instance`。done 示例：

```json
{
  "session_id": "original-session",
  "recall_id": "r-original-assistant-seq",
  "turn": 7,
  "status": "done",
  "assistant_message_id": "102",
  "assistant_reply": "这一次生成实际提交的最终回复"
}
```

turn 必须是 Ember 原响应/查询给出的轮号，不是 App 轮号、Heart turn_id 或当前新 turn。recall_id 沿用原值，不从其中的数字推测消息；assistant_message_id 从绑定原生成的真实消息读取全局 seq，不能换为重生成的兄弟回复、当前 leaf 或预分配但未提交的 seq。不补造用户消息，也不重发 turn_start。

error/cancelled 同样使用原 session_id、recall_id、turn 和实际 status；省略 assistant_reply、assistant_message_id（服务入口会按空正文/null 处理）。部分输出不作为 done，不填假正文或假 seq。已经实际完成的生成不能因为补投超时而改成 error/cancelled。

收轮请求没有新增 event_id 或幂等请求头，普通 MCP 的 Tool-Call-Id 不用于收轮。幂等身份由实例和 recall_id 确定，session/turn 用于归属校验。

### 3. 幂等、回执与重试

同一生成只确定一份终态请求：冻结 session、turn、status、最终正文和真实 assistant seq；后续原样重发。可以固定请求正文，也可以持久保存不可变消息引用/版本并校验内容后重建，不另行无限期复制聊天全文。删会话后按 App 生命周期停止补投，不绕过墓碑重建消息。

- 200 且 ok=true、归属和终态匹配：完成；重复同内容返回原回执，不增加 turn 或重复将回复加入活动语料。
- 超时/断线/5xx：保留原请求，独立退避重试。也可先查 `/ember/generations/{recall_id}`；已知 done/error/cancelled 后仍可用原请求重试，交由 end_hash 校验全部终态内容。仅查询得到同一 status/seq 不足以证明正文也相同。
- 409 id_conflict：原身份已收到不同终态内容，保留冲突并人工/程序对账，不覆盖、不换 ID 逃过校验。context_mismatch、invalid_identity/invalid_status 等同样排查原账，不用当前字段修补。
- 409 generation_not_ready：原登记尚不具备可收轮轮号，回查原登记状态，不伪造 turn。
- 404 unknown_recall：核对实例、原账和服务数据，不靠补发 start 或省略 recall_id 退回旧模式。

工具 JSONL、轮映射 JSONL 与 SQLite 终态回执并非一个事务。回执未保存的故障窗口可能重复追加同值映射行，因此这里承诺同身份/同内容的终态结果幂等，不宣称跨存储绝对只写一行。不重跑已发生的工具动作。

### 4. 过期、未知与不可用

| 原召回/查询状态 | 是否补收轮 |
|---|---|
| refs 200 ready，response_available=true/false | 已有真实终态就补；false 只是召回正文已过期 |
| response 查询或重复 start 为 410 response_expired | 改查 refs，确认 ready 后补；不需要取回 injected_text |
| 202 recall_in_progress / recall_recovery_pending、网络错误、5xx | 没有先前可靠开轮成功证据时，保留等待，不盲发 end；按原 ID 查询 |
| 409 recall_outcome_unknown | 没有先前可靠成功证据时，不自动补；保留原终态意图供后续对账，不把它改成取消，也不重跑原 ID |
| 409 recall_snapshot_unavailable | 若只有旧身份而无成功开轮/原 turn 证据，不自动补；若 App 已持久保存原成功 start 的真实 turn，可按原正常收轮任务重试，不需要快照正文 |
| 404 或身份/归属冲突 | 不自动补、不伪造开轮；记录不可用/冲突并对账 |

App 对召回 pending 的一天查询上限不是 Ember 取消生成的信号；到期可以停止自动查询并保留待核对状态，不凭超时制造 error/cancelled。已确认 ready 的收轮任务有独立的重试状态，不因召回正文 24 小时 TTL 或召回查询任务停止而自动丢弃。

当前底层 end 入口主要检查生成身份/turn，不替 App 判断“是否已有可靠开轮成功证据”。App 必须按上述状态门槛调用，不能因为某条 unknown 占位已有 turn 就当成成功召回。本回执不扩大 unknown 自动收轮能力。

### 5. 补发的实际效果与边界

补发记录原生成终态和 done 的真实回复 seq 映射。原生成仍是本进程当前 running 上下文时，正常收轮会追加其真实回复并关槽；已经 superseded/interrupted 或重启失去上下文时，仅补历史终态/映射，不往当前新生成语料插入旧回复，也不关闭新生成。

Ember 不持久复制整轮聊天正文，原文仍在 App。因此“补收轮”不等于重建重启前全部内存语料，更不等于自动创建长期记忆 Event。它不重新注入召回、不增加 Heart 刺激、不刷新召回时钟、不恢复旧工具写入权限、不训练耳朵。无需新增唤醒自动 writer。

App 可据本文补接持久收轮任务，并在隔离测试中覆盖“终态先到/ready 先到、正文过期、被新生成替代、重启、收轮回执丢失、unknown 不盲发”。真实三方联调与功能开关启用仍待用户安排。

本次复验：现有生成/快照 55 项测试通过；另用临时库组合实测正文到期→重启→refs ready→建立新生成→补旧 done→同内容重试/异内容冲突，确认原引用不改、新生成不受影响。未改业务代码、未新增测试文件、未重跑完整 191 项，未部署或做真实三方联调。

另提示 App §6.1：id_conflict 可以在 Ember 已接受原身份后出现，不能把所有“failed”都解释为“Ember 没有收下”。可以停止自动处理并标冲突，但须保留原身份供对账，不能用新身份重开来规避。
