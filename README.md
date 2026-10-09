# Ember v3 MVP — 第一阶段 + 第二阶段

> 状态：**第一阶段完成（2026-09-30）**。20/20 验收测试通过。
> 状态：**第二阶段完成（2026-09-30）**。T1–T8 全过（22 checks），真实闭环可用：聊天 → 自动写 Event → lexical recall 命中。
> 状态：**Phase 2.5 完成（2026-09-30 晚，momo 裁决）**。单模型 + 共用上下文 + 工具调用进历史。52 项测试全绿，真实 GLM 三项验证通过。

## 目标（momo 原话）

> 让 Ember 第一次真正做到：发生过一件事，以后提到它，它能被直接想起来。

## 已实现的纵向闭环

```text
对话 → 写入 Event → 生成 Recall Route(s)
→ 下一次输入命中（exact lexical lookup）
→ 返回完整 Event
→ PresentationContext 记录 recall trace
→ LLM 能看到"为什么想起这件事"
```

## 文件结构

```text
ember-v3-mvp/
├── ember/
│   ├── models.py    # Event / RecallRoute / RouteHit / RecallTrace / PresentationContext
│   ├── store.py     # JSONL 持久化 + 可重建 lexical 索引（cue → route_id → event_id）
│   └── recall.py    # DirectRecall（exact lexical）+ build_presentation
├── tests/
│   └── test_phase1.py  # 20 项验收测试
├── demo.py          # 端到端演示（数据持久化在 data/，--fresh 重置）
└── data/            # events.jsonl / recall_routes.jsonl（source of truth）
```

## 关键设计约束（全部来自架构文档，实现时严格执行）

1. **运行时字段禁止写进 Event**（§2）：`models.py` 里 `FORBIDDEN_EVENT_FIELDS` 白名单式拦截，`from_dict` 检测到 activation/brightness/weight 等字段直接抛 `ValueError`。
2. **索引是工程层，可重建**（§12）：source of truth 是两个 JSONL 文件；`EmberStore` 每次启动从存储重建 `cue → route_id` 索引。测试 8 验证了重开进程后索引一致、召回仍然工作。
3. **mode × matcher 正交**（裁决②）：schema 里两个字段都在；第一阶段 matcher 只实现 lexical。
4. **must / may**：must 命中是 anchor，may 命中是弱候选，都进 trace，排序 must 在前。
5. **Event 命中后整体可见**（§9）：`build_presentation` 返回 Event 完整内容，无维度裁剪。
6. **PresentationContext 一次性、不写回**（§10/§11）：recall 产出的所有运行时对象只存在于内存。
7. **为什么想起**（§10）：presentation 里每条记忆带 `why_recalled`（route_id / cue / mode / matcher / cue_type），trace 顶层带 `initial_anchors`（event_id / route_id / cue / match_type）。

## 明确没做的（第一阶段禁令）

Semantic Recall、embedding、Event Relation、Episode Cluster、Sleep、propagation、activation dynamics、LLM、fuzzy matching。

## 已知 baseline 取舍（不是已证明设计，铁律）

- `_extract_cues`：按空白/标点切词 + 整句子串匹配双路。中文分词很粗，但第一阶段只要求 exact lexical lookup，够用。真正的 cue 提取质量等有 LLM Memory Writer 时再谈。
- 多字 cue 的子串匹配可能会误命中（如 cue "v3" 会命中任何含 "v3" 的句子）。这是 exact lexical 的固有宽度，属于 baseline 假设，留给 Sleep 收紧。

## 运行方式

```bash
# 验收测试
python tests/test_phase1.py

# 端到端演示（首次写入，之后可重复召回；--fresh 重置数据）
python demo.py [--fresh]
```

## 下一阶段候选（等 momo 安排）

- ~~LLM Memory Writer（写入时自动产出 Event + Routes）~~ → 第二阶段已做
- Semantic matcher（embedding 粗召回索引 RecallRoute 的 cue，LLM 确认）
- Recall Context 注入（proxy 已预留 `recall_context` 开关）
- Relation / Cluster / Sleep（第二、三阶段）

---

# 第二阶段：Memory Writer + Proxy + 测试前端（2026-09-30）

## 实现的闭环

```text
浏览器测试前端 → Ember Proxy → Chat LLM → 正常回答
                    ↓（response 完成后）
              Memory Writer LLM → create_memory_event tool call
                    ↓ 严格 schema 校验
              Ember Store（Event + Recall Routes，系统字段由 Proxy 生成）
```

## 新增文件

```text
ember/
├── llm.py      # OpenAICompatClient（标准库 HTTP）+ MockLLMClient（无 Key 测试）
├── writer.py   # Memory Writer system prompt / tool schema（momo §5-§7 逐字原文）+ 严格校验
└── service.py  # EmberService：chat 转发 + memory write 流程 + 失败隔离（HTTP 无关）
proxy.py        # HTTP 层：POST /v1/chat/completions（OpenAI 兼容）+ /v1/ember/* 观察端点
web/            # 测试前端：左对话 + 右 Memory 面板（Event/Routes/完整 JSON 弹窗）+ 调试区 + 手动 recall
tests/test_phase2.py  # T1~T8 验收
```

## 关键实现决策（均为 baseline，不是已证明设计）

1. **失败隔离（T8）**：Memory Writer 在 try/except 内运行，任何 LLMError/校验失败只落 `memory.status=failed`，Chat response 不受影响。
2. **Writer 上下文最小化（§4）**：只给本轮 user+assistant + 之前最近 2 轮背景，绝不给整个 Store。
3. **系统字段生成（§8）**：event_id / version_id / source_ref / raw_ref / event_time / ingest_time / turn_refs / route_id / status / provenance 全部由 Proxy 生成；校验器拒绝 LLM 提供任何额外字段。
4. **matcher 落地策略**：第二阶段所有 route 的 `matcher="lexical"`（semantic 未实现）；Writer 给的 conceptual/situational cue 保留 cue_type 原值、按 cue 文本做 exact lookup——文本逐字出现就命中，语义命中留给下一阶段。这是明确的 baseline 取舍。
5. **一 Tool Call 一 Event（§9）**：schema 层面强制（required 恰好 event + recall_routes）。
6. **前端只读（§13）**：能看 Event 详情/Routes/raw_ref，没有任何编辑入口。
7. **Mock Provider**：`EMBER_PROVIDER=mock`（默认）时 Chat/Writer 都是本地 mock，Chat 按触发词（决定/定稿/改成/完成/发布/上线/采用）扮演 Writer，用于零 Key 跑通全流程和测试。

## 运行方式

```bash
# Mock 模式（无 Key，流程演示）
python proxy.py
# → http://127.0.0.1:8765/

# 真实 LLM（任意 OpenAI-compatible 端点）
set EMBER_PROVIDER=openai
set EMBER_CHAT_BASE_URL=https://api.example.com/v1
set EMBER_CHAT_API_KEY=sk-...
set EMBER_CHAT_MODEL=glm-5.3-flash
python proxy.py
```

---

# Phase 2.5：单模型 + 共用上下文 + 工具调用进历史（2026-09-30 晚）

momo 四条裁决，全部落地：

1. **聊天和写记忆用同一个模型**：`EmberService` 砍掉 `writer_client/writer_model`，只有 `client + model`。chat 和 memory writer 是同一次会话里的两个角色，不再有两套模型配置。
2. **切换模型共用一个上下文**：前端 header 加模型下拉框（glm-5.3-flash / glm-4.7），`model` 随每次请求发送；session 历史是唯一上下文真相源，换模型只换请求参数，历史原样延续。真实验证：glm-5.3-flash 聊两轮 → 切 glm-4.7 → 上下文完整接上（turn 连续、能引用前文）。
3. **工具调用进模型上下文**：writer 若调用 `create_memory_event`，Service 把 `assistant(tool_calls)` + `tool(结果 JSON)` 两条消息追加进 session.messages。下一轮起模型自己知道"我调用过工具、结果是 created EVT-xxx / failed"。真实验证：模型准确说出工具名、event_id、写入内容。skipped（未调用工具）不追加任何消息，历史如实。
4. **glm-4.7 不稳就用 glm-5.3-flash**：默认单模型 `glm-5.3-flash`（writer 也用它）。实测 glm-5.3-flash 的 tool arguments 比 glm-4.7 稳；坏 JSON 的宽容解析 + 重试逻辑保留作为兜底。

新增 `/v1/ember/session` 调试端点（POST，body 带 session_id）：查看当前 session 的完整模型上下文（含 tool_calls/tool 记录）。前端每轮回复显示 Tool call 徽章（`create_memory_event → success/failed（已写入模型上下文）`）。

测试：T0/T0b 新增（单模型、工具记录进历史、切模型延续上下文），52 项全绿（phase1 20 + phase2 32）。
```

环境变量：`EMBER_PORT`（默认 8765）、`EMBER_DATA_DIR`（默认 ./data）。

## 验收结果

```text
T1 写入普通事件          ✓（created + 3 routes，系统字段全由 Ember 生成）
T2 普通闲聊不写          ✓（skipped，writer 确实被调用）
T3 多轮对话不机械拆分    ✓（前两轮不触发，最后形成有 outcome/change 的单条 Event）
T4 Route 覆盖后命中      ✓（recall("Ember") 命中，why_recalled 完整）
T5 must/may 可观察       ✓（trace 中 must+lexical / may+lexical 可区分）
T6 原文可回溯            ✓（raw_ref = conversation/session-xxx/turn-N）
T7 Store 重启            ✓（Event/Routes/索引重启后一致且可召回）
T8 Writer 失败隔离       ✓（chat success + memory failed）
另：第一阶段 20 项回归全部通过。
```
