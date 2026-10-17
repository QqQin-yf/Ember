# BRIEFING_SPEC — 简报对接规格（2026-10-17 设计夜定稿）

> 交付对象：Deliz 侧（app 编排）。ember 侧实现已完成，本文是契约全文。
> 简报 = 拼装式（ember 查表组装，**零 LLM、零 token 成本**），截断驱动。

## 1. 它是什么、什么时候发生

- 用户上下文采用**丢头留尾式截断**：截断时头部丢弃、尾部（最近一天左右的对话）留在上下文。
- 旧简报文本在头部——**随截断一起丢掉**。所以每次截断时生成的新简报必须**自包含**（重新带上所有还活着的认知），app 拿到后注入到留下的上下文开头。
- 断线重进/新会话开场：app 随时可用 GET 接口取当前简报重新注入。

## 2. 接口

### 2.1 POST /ember/briefing/refresh（截断时调用）

请求体：

```json
{"session_id": "main", "keep_from_turn": 458}
```

- `session_id`：必填。
- `keep_from_turn`：**现场起点轮**——截断后留在上下文里的最早一轮。现场区间的记忆一律不进简报（原文就在上下文里，带了就是复读）。传 0 或缺省 = 不做现场排除。

响应 200（`text` 就是注入文本，直接贴上下文开头）：

```json
{"status": "ok",
 "text": "【近况】\n◆ ……",
 "chars": 2381, "session_id": "main",
 "stats": {"pinned": 1, "sig10": 1, "new72": 8, "recalled_top": "EVT-014-ab12cd",
            "dropped": {"core": 0, "summary": 2}}}
```

### 2.2 GET /ember/briefing（取当前简报）

返回最近一次 refresh 的结果原样；从未刷新过 → `{"status": "empty"}`。

### 2.3 钉住

- 工具：前端 AI 的 `pin_memory` / `unpin_memory`（用户明确说「钉住」时调）。
- HTTP：`POST /ember/memory/pin`、`POST /ember/memory/unpin`，体 `{"event_id": "..."}`，幂等。
- 联动：钉住的**必进简报（原文层）**且**自动召回不再浮现**（search 主动检索不受影响）。

## 3. 入选与分层（ember 拼装规则，app 无需关心，列出供理解）

| 类 | 入选 | 层次 |
|---|---|---|
| pin + significance 10 | 必进（现场除外） | **原文**：core + ±3 轮原文（事件是真区间则按区间；单句扩 ±3 轮，不足有多少拉多少；原文不可达退 key_quotes） |
| 72h 新增（现场外） | 都要 | significance 8-9 → **core**；1-7 → **summary 一句** |
| 召回过的旧记忆 | **只放最相关一条**（被召回次数最多，平局取最近） | core |
| 现场尾部（≥ keep_from_turn） | **不进** | —— |
| 观察事件（origin=observe） | **不进** | ——（习惯不是「正在进行」） |

- 层次依据 = 写入时前端 AI 标的 `significance`（**2026-10-17 起格式为 `"N：一句话理由"`，N∈1-10**，写入端硬校验）。旧事件无数字前缀按 5 计。
- 预算 **6000 字**（`EMBER_BRIEFING_BUDGET` 可调）：超了从低往高砍——先砍 summary 行，再按 significance 从低砍 core 行；**pin 和 10 分永不砍**。`stats.dropped` 如实报砍了几条。
- 去重：同一事件在一份简报里只出现一次（类间取最高层）。

## 4. 与召回的去重（ember 内部已实现，app 无需动作）

- 简报注入 = 一次「在场」：简报带过的事件记入在场账（ledger route=briefing），**之后 2h 内被召回时只走轻量行报到**，不重复展开。
- 钉住的事件：永久不自动浮现（既有裁决），简报是它们唯一的常驻出口。

## 5. app 侧要做的

1. 截断动作处：先算出留尾的起始轮 → 调 `POST /ember/briefing/refresh` → 把 `text` 注入留下上下文的开头（标注来源「近况简报」即可）。
2. 新会话/断线重进：调 `GET /ember/briefing`，非 empty 就注入。
3. 用户在 UI 钉/取消钉记忆（如果做 UI 的话）：调 pin/unpin 端点。
4. 前端 AI 的写入行为有变：`significance` 必须是 `"N：理由"` 格式（写入端会拒），participants 别漏 ai。
