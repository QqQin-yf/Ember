# ember 接口文档

> ember：旁路记忆系统。职责边界：记忆写入（模型自主+校验）、召回注入、sleep 夜间维护（挂关系/结构维护）、沉淀证据账本；
> **不管**对话主路径——ember 挂了/未部署，聊天照常，只损失记忆能力。
> 消费方：app（钩子组 + sleep 编排 + 沉淀证据转交）、模型的工具循环（MCP 工具组）。
> （梦已归 app 的 dream_submit 工具，ember 不再向 heart 推送——旧梦通道 2026-10-15 退役。）

## 1. 基础信息

- 协议：HTTP/1.1 + JSON
- 默认地址：`127.0.0.1:8767`（sidecar 形态）
- 鉴权：配置 `EMBER_TOKEN` 后所有端点要求 `Authorization: Bearer <token>` 精确匹配（MCP 与钩子一视同仁）；未配置放行
- 接入形态：旁路（2026-10-12 拍板）——前端拥有对话循环，ember 只被钩子调用与 MCP 工具调用

## 2. 通用约定

### 2.1 失败如实回执，绝不编造

工具调用/核对失败一律返回 `{"status": "failed", "error": "<原因>"}`——不猜测、不兜底、不编造数据。引用的句子在语料里找不到就拒绝写入（双盲纪律）。

### 2.2 stale 拒收

用户连发消息时，慢到的旧轮 `turn_end` 会被拒收（`{"ok": false, "reason": "stale turn_end …"}`）——不污染新一轮的出处账。turn 计数允许留洞（缺轮后钩子的轮次不存档）。

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

- turn_end 的 turn 校验：与 turn_start 发的号对不上 → stale 拒收
- 同会话连续两次 turn_start（重新生成）→ 覆盖旧上下文

## 4. 钩子组（消费方：app）

### 4.1 POST /ember/hooks/turn_start

开新一轮 + 本地召回，返回记忆注入文本。

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| session_id | string | — | 会话标识（缺省默认会话） |
| message | string | ✓ | 本轮用户消息 |
| heart_turn_id | string | — | heart 的 `t-XXXXXX`（对账映射） |
| user_message_id | int | — | 本条消息在后端消息树的行 id（写入记来源） |
| context_start_id | int | — | 当前窗口起点（后端 message_id）：来源全在窗口内的记忆不再召回 |

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

### 4.2 POST /ember/hooks/turn_end

收轮 + 补记回复行 id。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| session_id | string | — | |
| assistant_reply | string | ✓ | 最终回复 |
| turn | int | — | turn_start 返回的 turn 号（stale 校验） |
| assistant_message_id | int | — | 本条回复在后端消息树的行 id |

响应 200：`{"ok": true, "session_id": "…", "turn": 7}`

## 5. MCP 工具组（消费方：模型的工具循环，经 app 转发）

端点：`POST /mcp`（MCP Streamable HTTP）。工具清单 `tools/list` 返回三件；以下为语义契约。

### 5.1 create_memory_event

记录一条经历。**只在出现具体的、值得留存的经历时调用**；同一条经历只调一次（重复调用会产生重复记忆）。

必填参数：`event`（对象：summary/participants/objects/context/core/outcome/state/affect/significance/details/key_quotes）、`recall_routes`（召回线索）、`quote_spans`（出处句对：起尾句逐字摘自原文）。

校验与回执：

- `quote_spans` 全部定位不上 → **整条拒绝**（`failed`，指路重摘）——双盲纪律
- `key_quotes` 逐字核对，对不上的条目丢弃并进 `dropped_quotes`（记忆本体已保存，不因摘录出入重试）
- 出处（会话/轮次/heart 钉子/message_id）由进行中的一轮自动关联，模型无需提供

成功回执：`{"status": "created", "event_id": "EVT-…", "summary", "raw_ref", "route_count", "recall_cues" [, "dropped_quotes"]}`

### 5.2 read_memory_source

翻一条记忆对应的对话原文（只读）。只在需要核对原话细节时调用。语料 = 内存会话 ∪ 向后端原文接口按需拉取（用完即弃，本地不落盘）。

- 成功：`{"status": "ok", "event_id", "turns", "transcript", "truncated"}`
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

## 6. Sleep 夜间维护（消费方：app 编排）

用户在 app 侧按键进入 sleep（人为编排，与 heart 入睡档同构）后，app 按固定顺序编排两个阶段：**挂关系**（link，借窗口跑）与**结构维护**（structure，ember 自发第三方 LLM）。

> **编排顺序（务必对 app 讲清）**：挂关系清完**不等于没活儿了**——之后还有一步 `structure/run`。必须等 link 交卷清零（progress 归零）后再调 structure/run；漏调会留下散事件归属悬空、旧 cue 不收紧。

```
进入 sleep（app 按键）
  ① GET  /ember/sleep/link/batches     → 材料出队（≤5 批）
  ②    app 把材料贴进窗口末尾，正主 AI 判联（link_relations）
  ③ POST /ember/sleep/link/submit      → 交卷落库（pending 扣减）
       （断线重进：GET /ember/sleep/link/progress 查进度，材料照旧）
  ④ ①-③ 循环到 progress 归零
  ⑤ POST /ember/sleep/structure/run    → 结构维护（散事件归属 + cue 收紧）
  ⑥    app 判 dream / 沉淀（走 app 自己的工具与库）
       → app 真的存下一版新沉淀时：POST /ember/sleep/sediment/evidence
```

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
           "core": "…", "summary": "…"}
        ]}
    ]}
  ],
  "pending_total": 3,
  "materials_total": 3
}
```

- `old_events` 的实体标注（entity_name/entity_id）是 attach 校验的依据，判联材料必读
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

结构维护（无请求体）。ember 自己发第三方 LLM：收集全库尚无实体归属的散事件，判定 extract（立新户，≥3 条散事件）/ attach（归入实体库已有实体）；随后对闲置旧 cue 跑收紧（cue_tighten）。

响应 200：

```json
{"strays_total": 5,
 "saved": [
   {"action": "extract", "name": "…", "entity_id": "ent_…",
    "entity_action": "extract:ent_…(名) x3"},
   {"action": "attach", "entity_id": "ent_…", "members_patched": 2,
    "entity_action": "attach:ent_…"}],
 "rejected": [{"action": "extract", "name": "…", "errors": ["extract member 不足 3: 2"]}],
 "cue_tighten": {"checked_events": 10, "retired": [], "skipped": []}}
```

- 校验逐条进行：member/event 必须在本次散事件清单内（清单外=幻觉，拒绝该条）；同一事件只认领一次（单归属）；extract 名字不与已有实体重复
- **幂等可重试**：已归属的自动出清单；LLM 调用/解析失败会 5xx，直接重试即可
- 无散事件时跳过 LLM，只跑 cue 收紧

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

- `from` / `to`：**message_id 整数区间（含两端）**——对外契约只有 message_id 一套（见 §8）

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

## 8. 坐标系说明

对外契约**只有 message_id 一套**（后端消息树行 id）：钩子请求携带、事件出处经账本换算回指。

内部坐标（app 无需关心）：ember 的 `turn-N`（会话内序数）与 heart 的 `t-XXXXXX` 各自独立发号、经 `turn_ledger.jsonl` 旁路账本互相映射并指向 message_id——换算是 ember 内部事务，Event 本体不存这些映射（旁路文件，可重建）。

## 9. 配置清单（.env，启动时自动加载）

| 变量 | 说明 |
|---|---|
| EMBER_HOST / EMBER_SIDECAR_PORT / EMBER_TOKEN | 监听与鉴权（跨机部署 EMBER_TOKEN 必设，不设 = 不拦） |
| EMBER_DATA_DIR | 记忆库目录（默认 data/） |
| EMBER_SOURCE_URL / EMBER_SOURCE_TOKEN / EMBER_SOURCE_TIMEOUT | 原文提供接口（heart-chat `/api/source`，将来 Deliz 同 schema 接管——契约见 §7.1） |
| EMBER_EMBED_BASE_URL / EMBER_EMBED_API_KEY / EMBER_EMBED_MODEL | 向量服务 |
| ZHIPU_API_KEY / EMBER_SLEEP_MODEL | Sleep 管线的 LLM |
| EMBER_STRUCTURE_MODEL | 结构维护归属判定模型（缺省回落 EMBER_SLEEP_MODEL） |
