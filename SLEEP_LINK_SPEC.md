> 历史规格：旧 Sleep 挂关系/结构维护入口已退出。当前职责、迁移及多余步骤说明见 [MEMORY_LIFECYCLE_SPEC.md](MEMORY_LIFECYCLE_SPEC.md)。

# Sleep 挂关系（link）判联规格 —— 给窗口判联 AI

> 消费方：app 侧编排（点 sleep → 出材料 → 贴窗口 → 判联 → 交卷）。
> 本文是判联 prompt 与 verdict 的**唯一完整定义**；端点形状见 `API.md` §6。
> 判联 prompt 是 momo 逐字审过的定稿（`ember/prompts.py` LINK_SYSTEM_PROMPT），
> **必须原样使用，不得改写**。

## 0. 流程位置

```
① GET  /ember/sleep/link/batches   → 拿材料（≤5 批）
② app 把【判联 prompt 全文 + 材料】贴进窗口末尾，窗口 AI 输出 verdict
③ POST /ember/sleep/link/submit    → 交卷：{source_message_id, results:[{material, verdict}]}
④ 循环到 GET /ember/sleep/link/progress 归零
```

交卷时 `material` 必须**原样回传**（校验基于回传材料）。

## 1. 材料形状（出队接口给的，判联 AI 只能看到这些）

```json
{"new_id": "EVT-014-ab12cd",
 "new_event": {"event_id": "EVT-014-ab12cd", "core": "新事件完整叙述", "summary": "新事件摘要"},
 "entity_name": "（新事件所属实体名，可能为空串）",
 "old_events": [
   {"event_id": "EVT-003-xyz987", "entity_name": "子夜阁项目", "entity_id": "ent_ziye",
    "entity_summary": "（该实体当前的名片描述，可能为空串）",
    "core": "旧事件完整叙述", "summary": "旧事件摘要"}
 ]}
```

- `old_events` 可能多条，也可能只有一条；每条是"该新事件写入时被召回带出的旧事件"
- `entity_name` / `entity_id` 标注是 attach 分支的合法性依据（attach 只能指向材料里出现过的实体）
- `entity_summary`（2026-10-17 Claude 提议）是该实体**当前的名片**：attach 改写 summary 时**在旧名片基础上修订**，不要从零重写——材料只含这次召回浮起的少数成员，照眼前几条从头写会把旧名片积累的内容冲掉

## 2. 判联 prompt（原样全文）

```text
材料中每条旧记忆可能标注了它所属的实体（Entity）。注意：联系边只看两件事的内容之间有没有具体的、可以指出来的联系，与归属无关——标注了归属的旧记忆，无关的照样判无关系。

第一部分：联系裁决（新记忆 × 每条旧记忆，逐条给出）三种结局，每条旧记忆独立判定：

一、relation（有具体联系）
类型从词表选最贴切的一个（边方向固定：旧事件 → 新事件，每个类型读作"新事件 <type> 旧事件"）：
- continues：新事件是旧事件的直接延续
- concludes：新事件给旧事件画上句号
- inspired_by：新事件由旧事件触发或启发
- caused_by：新事件起因于旧事件（"旧事件引起新事件"也用它）
- responds_to：新事件回应旧事件的问题或观点
- resolves：新事件修正、推翻或消解了旧事件的核心结论
- revisits：新事件重新处理旧事件的同一主题且有实质进展
- contrasts：新事件与旧事件立场或结论相对照
- related_to：确有联系，但以上类型都不贴切
判 resolves 有特殊效力：即认定旧事件已被推翻。此时必须同时给出：
- change：以旧事件主人公的第一人称，补写"这段经历之后，我的认知发生了什么变化"。只写心路，禁止写"此条已作废"之类的效力声明。
- status_evidence：一句话依据，说明新事件如何修正了旧事件。

二、neighbor（普通邻居）
共现于同一场对话或同一话题空间，但没有具体的、值得单独记录的联系。"都在谈同一个大话题"不算联系。判 neighbor 不产生任何结构。

三、none（无明显关系）

判定纪律：
- 从严判 relation：必须有具体、可指出的联系。"主题相近"不是联系。
- 拿不准 relation 还是 neighbor 时，判 neighbor。
- 拿不准是不是 resolves 时，判更弱的类型；只有核心结论确实被推翻/修正/消解才算 resolves。

第二部分：归属动作（整个材料判定一次，三选一）

一、extract（提取开新户）——同时满足以下全部条件：
- 这批事件（新+旧）围绕同一个对象/主题；
- 其中尚无实体归属的散事件 ≥3 条；
- 这个对象不是材料中任何已有实体的子题（若是，不提取，改用 attach）。
输出：name（从事件原文提炼，必须是专名，不用普通词汇）、aliases（对话中真实出现过的其他称呼，照原文）、一句话说明、member_event_ids（只列尚无归属的散事件，已归属的不进）、summary（这个实体的第一版描述：三到六句，像一张名片——它是什么、包括哪些方面、目前状态；是提炼不是汇编，不按事件罗列、不写变化过程）。

二、attach（归入已有实体）——新记忆讲的正是某条旧记忆所属实体的事时使用：输出该实体的 id。只有新记忆尚无归属时才可用。归入时当场改写实体描述：对照名下全部成员输出新的 summary（三到六句，同名片标准）；并顺带判断目标实体是否已经收尾（相关的事做完、结束或明确不再继续）：收尾了输出 "close": true 和一句话 close_reason；还活跃或拿不准就 "close": false。

三、none（不做归属动作）——主题不聚焦，或只是跨主题的零星联系。不提取也不归入时什么都不输出，事件保持散着。

输出格式（只输出 JSON，无任何其他文字）：
{
  "pairs": [
    {"old_event_id": "<原样回填>",
     "outcome": "relation | neighbor | none",
     "type": "词表之一，仅 outcome=relation 时填，否则留空",
     "confidence": "high | medium | low",
     "reason": "一句话依据",
     "change": "仅 type=resolves 时填，否则留空",
     "status_evidence": "仅 type=resolves 时填，否则留空"}
  ],
  "entity": {
    "action": "extract | attach | none",
    "name": "仅 extract 时填",
    "aliases": ["仅 extract 时填"],
    "description": "仅 extract 时填，一句话",
    "summary": "仅 extract/attach 时填：实体描述（立户首版 / 对照全部成员的改写版）",
    "member_event_ids": ["仅 extract 时填"],
    "entity_id": "仅 attach 时填，目标已有实体",
    "close": "仅 attach 时填：true / false",
    "close_reason": "仅 close=true 时填，一句话"
  }
}
```

## 3. verdict 逐字段说明与硬校验（交卷时服务端逐条检查，违反即整份拒绝）

### pairs（必须覆盖材料里每一条 old_events，且只允许指向材料内旧事件）

| 字段 | 规则 |
|---|---|
| `old_event_id` | **原样回填**材料 `old_events[].event_id`。指向材料外 = 幻觉，**整份拒绝**；同一旧事件重复出现也拒绝 |
| `outcome` | 三选一：`relation` / `neighbor` / `none` |
| `type` | 仅 `outcome=relation` 时填；词表 9 个（见 prompt）。校验词表是超集（另含 `causes`），但 **prompt 未提供，不要输出** |
| `confidence` | 仅 relation 时校验：`high` / `medium` / `low` |
| `reason` | 一句话 |
| `change` + `status_evidence` | **仅 `type=resolves` 时必填且非空**，缺任一即拒绝；非 resolves 不填 |

`neighbor` / `none` **不得带 `type`**（带了即拒绝）。

### entity（整份材料一个对象，`action` 三选一）

> **2026-10-17 起 entity summary 由窗口 AI 当场写改**（glm 结构维护
> 归属与夜批重算退役）：`summary` 字段加入 verdict——extract 必填
> （实体第一版描述，3-6 句，像名片、不写变化过程）、attach 必填
> （对照名下全部成员的改写版）。宽松校验：未输出/空 → 实体保旧值，
> 不拒整份。散事件主题延续时召回自举（同话题必然浮起），无需全库扫。

| action | 必填 | 硬校验 |
|---|---|---|
| `none` | 无 | 不输出其他字段 |
| `extract` | `name`、`aliases`、`description`、`summary`、`member_event_ids` | `name` 不得与库中已有实体重名（大小写不敏感）；`member_event_ids` **≥3 条**且每条必须是**尚无归属的散事件**；名单不含已有归属的事件 |
| `attach` | `entity_id`、`summary`、`close`（`close=true` 时加 `close_reason`） | `entity_id` 必须是**材料内某条旧事件标注的 `entity_id`**；新事件已有归属则不能 attach（单归属铁律）；`close` 取 `true`/`false`（字符串 `"true"`/`"false"` 也收） |

### 常见拒绝原因速查

1. pair 指向材料外的事件（编造/幻觉 id）
2. pair 重复（同一 old_event_id 出现两次）
3. relation 缺 `type` / `type` 不在词表；`confidence` 缺或非法
4. `resolves` 缺 `change` 或 `status_evidence`
5. `neighbor`/`none` 带了 `type`
6. `extract` 重名 / member 不足 3 / member 已有归属
7. `attach` 目标不是材料内旧事件的实体 / 新事件已有归属 / `close=true` 缺 `close_reason`

### 空材料怎么判（`old_events` 为空数组）

候选旧事件 = 新事件出生的那几轮对话里**被召回浮出过的**旧事件——空库冷启动或全新话题的第一件事，那段对话里可能什么旧记忆都没浮起。此时材料照出，`old_events` 为 `[]`：

- `pairs` 给空数组 `[]`（没有旧事件，无从判联）
- `entity` 只能选 `extract` 或 `none`——attach 需要材料里出现过的实体 id，空材料下没有，选了会被校验器整份拒绝
- 同批多个空材料可以合起来看：几条新事件围绕同一对象时，extract 把它们一起立户（member ≥3 且都是无归属散事件）

## 4. 交卷（app 侧动作）

```json
POST /ember/sleep/link/submit
{"source_message_id": 123,
 "results": [{"material": {"…出队材料原样…"}, "verdict": {"…上面格式…"}}]}
```

响应里 `saved` / `rejected`（附 `errors`）逐条给出；`remaining_pending` 非空就继续下一轮（断线重进用 `GET /ember/sleep/link/progress` 查，材料幂等不丢）。**link 清零后还有一步 `POST /ember/sleep/structure/run`**，漏调会留散事件悬空。
