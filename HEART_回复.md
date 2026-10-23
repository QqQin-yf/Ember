# Ember 对 Heart / Deliz 第二阶段对接的回复

## 日常旧模式历史摘录修复（2026-10-23）

修复旧 hook 模式没有新生成账就丢失历史语料的问题：从旧原始逐条编号记录取候选，仍由精确路径端点对照本轮锚点核验；冲突编号不取 max，错误分支不会污染原文，也不会使正确路径全部失效。连接/鉴权/路径/缺编号与真正 quote_not_found 分开回执，取材过程不打开第二阶段开关。loopback/Tailscale 源请求绕过公共代理。

真实失败的两份摘录已在临时库配真实 App 原文重放通过，未写生产记忆；完整 237 项 VPS 隔离回归通过。App 原文服务须保持运行，本次用户启动 App 后连接已恢复。详见 QUOTE_SOURCE_COMPAT_FIX.md；独立令牌切换/通用令牌轮换仍单独保留，不因本次修复自动完成。


## App f239b21 / §13：隔离原文进程联验通过（2026-10-21）

已按指定专用凭据在实际 Ember 8878 服务进程加载 8792 原文配置（timeout=5，loopback 显式绕过代理）。18 条 HTTP 样本、14 条实际 source_client 用例、2 条无/错令牌、7 条令牌范围检查通过；监听后外部经标准 MCP 再验 10 条，正确原文逐字一致，拒绝均 not_archived 无 transcript。进程内自检发生在实际监听 PID 启动阶段；当前版本/PID 看 D:/phase2-secrets/ember-readiness.json。具体记录及边界见 SOURCE_PROVIDER_PROCESS_VALIDATION.md。

当前实例归属模型满足本轮要求：换实例旧方不可读；未启用 Ember 的 default 归属会话仍可读，不额外承诺历史投递授权或“关 Ember 即撤权”。隔离的旧接口歧义 409/实例归属缺口可按该范围关闭，**生产提供方配置、部署与令牌切换仍未完成**。

只读核对确认本机与 VPS 日常 Ember 的原文配置都曾放入日常 App 通用 DELIZ_TOKEN；未改配置、未轮换。19:43–19:50（台北时间）VPS journal 为 0 条，已知本地日志不覆盖该窗口；没有可核对的错误记录，不能证明当时没有请求或失败。生产轮换继续按 App §13.3 单独安排。

本轮未推送、未部署/重启 VPS、未开启生产开关。此前“App 忽略实例头/8878 未配置 source”等为历史状态，以本段及验证文档为准。


## 精确读取验收完成与生产配合（2026-10-21）

Ember 已完成 `/api/source/path` 迁移，覆盖事件工具、召回内联、briefing/sleep、引文定位核验和 observe；无旧 range 回退。真实 App index.ts + 临时合成库 15 项通过；224 项 unittest 通过，5a 四个进程强杀检查点恢复通过。详细范围、命令和降级边界见 SOURCE_PATH_VALIDATION.md。

**给 App 的迁移放行回执：可以开始将旧 `/api/source` 歧义请求改为 409。** 各环境先确认 Ember 新客户端已加载再发布收紧；VPS 尚未部署，不要因本机验收通过就先收紧生产旧调用方。App 改完仍需补验旧请求确为 409 与无 fallback。

**#3 尚未关闭：** 本次真实 App 发现 source 路由忽略 `X-Heart-Instance`，未知实例头也能读已知会话。App 需落实实例/会话归属或可验证的独立路由隔离；再提供真实部署基址、安全凭据文件路径、实例/库映射及合成分支样本，联合实测鉴权、跨实例/会话拒错与重启配置生效。逐项清单见 PRODUCTION_PREREQUISITES.md。不把测试服务的 source 未配置降级当成生产实配通过。

旧无编号来源、邻近轮次缺路径、混合分支增量取材均不猜；观察不推进游标。#5c 保留有范围的后续补测，生产/唤醒开关未批准、未开启。下方“迁移/强杀未完成”等是历史记录，以本段及验收文件为准。


日期：2026-10-20。核对基线：本仓库提交 `a5a7c8f`。

核对文档：`D:/your-heart/EMBER_唤醒对接.md`、`D:/your-heart/唤醒三方契约.md`、`D:/your-heart/HEART_对Ember回复.md`。初次只读核对按 a5a7c8f / v0.5 记录，后续局部实现见下节；未部署或调用生产服务。第 1 节起保留初始核对和建议的历史语境，当前实现以顶部交付状态及配套 SPEC 为准；主动 run 建议仍未实现。

## App §12.3 生产前置：Ember 结论（2026-10-21）

已按用户转达记录 Heart 接收 §11/§12；生产开启仍未批准。按保留历史原文和记忆工具的当前范围，Ember 将 #2 旧 source 精确路径迁移、#3 生产原文提供方实际连通/鉴权/实例与路径拒错核验、#5a 事务中途强杀恢复列为开启前必须补齐，均尚未完成。#5c 完整三方异常组合可在现有拒错纪律和明确验收范围下后续补测，不冒充已通过；#7 部署/用户批准仍必须。

依据、消费者清单、迁移顺序、强杀验收标准和限制见 PRODUCTION_PREREQUISITES.md。旧原文错读不是单纯 unavailable，不能凭元数据 resolved 联调通过而豁免。上述为 Ember 工程分类，不代替 Heart/用户确认三方完整清单；本次仅文档，不改服务、生产配置、开关或 backfill 协议。

## App §11 真实验收接收与覆盖边界（2026-10-21）

已读 App a7d82f3 / §11，接收其历史补交、迟到 resolution、首次 resolved 与幂等三方对账通过报告；本次未重跑三方。多轮来源修正符合现有契约，无新增 Ember 接口阻塞。Ember 相关 77 项复跑通过，逐项覆盖表与集中缺口见 SOURCE_COMPLETION_COVERAGE.md。

需保留两项测试缺口：补交事务中途强杀进程尚未做（现有为异常回滚/服务对象重建和提交后真实重启）；Ember 尚缺完整历史轮＋当前轮补交的自动正向用例（App 已真实验通）。不把这些说成已发现的协议故障，也不把本地反例覆盖写成真实三方故障验收。本次只更新文档，服务仍为 3e62af6，生产开关和调度不变。

## 来源完成绑定已实现（2026-10-21）

按 SOURCE_COMPLETION_SPEC.md 实现独立完成绑定和来源候选修订；原快照/响应不改，只对原成员/出处身份精确匹配的缺失位置补 seq，同 done 重试或重启不重复推进。213 项测试通过；隔离服务加载身份和真实探针结果见 D:/phase2-secrets/ember-readiness.json，App 补验安排见 APP_交接回复.md 顶部。

App 需接新证据并补验真实 resolved 与迟到 resolution；Heart 不需新字段/接口。已知悉旧队列重启补投由 App 明确标 backfill，Ember 本次不新增相关协议。原文读取限制、生命周期和主动 run 仍不在本次交付。下方“仅方案、来源修订为 0”属于实现前历史记录。

## App §9 真实联调反馈与出处补交方案（2026-10-21）

已读 App §9，普通生成/召回、工具身份、迟到收轮、App 重启补投等通过项按其三方对账报告登记；App ecd29c6 已修复跨数据库 recall_id 撞号。resolved 出处和真实迟到解析尚未验通，原文提供方未配置仍单列，不据此宣称全部二阶段完成。

Ember 接受在同生成 done 后提供独立的来源轮完成绑定，详见 SOURCE_COMPLETION_SPEC.md：原出处与召回 snapshot/response 冻结，确切匹配原来源轮的新证据只进入来源辅助；已包含该事件的 recall 在候选实际变化时推进 sources_revision。App 接新证据后仍自行验证父链、发送独立 resolution；Heart 无需新增字段/接口。最终回复不能被追认为记忆写入时已存在的引用证据。

本次仅方案回执，运行服务仍为 156f29e，未实现修订更新或重启服务；不扩大生产开关、调度或自动 writer。§9 的重启补投 delivery 规则继续由 Heart/App 确认，Ember 不代为裁定。

## App §7—8 后的隔离交付（2026-10-21）

已核对 App b929d44 与集中回执。Ember 按用户授权提交快照/普通生成/补收轮相关实现，启动本机独立数据和 Bearer token 的真实隔离 sidecar（127.0.0.1:8878）；说明见 PHASE2_INTEGRATION.md，运行身份见本次交付消息与私有目录 runtime.json。本段取代下方“尚未提交”的历史状态，不代表生产部署或三方验收。App 完成水位的真实 Heart 验证按其 §8 保留，Ember 未代为重跑 Heart。

## App §6 接线回执与迟到收轮约定（2026-10-21）

已读 App 集中回执 §6，核对提交 d62aad1 的普通生成接线；App 已报告假服务验证完成，真实三方联调未做、两个功能开关仍关闭。Ember 明确要求：当原召回恢复为 refs ready 且原生成真实终态已提交时，App 应用原身份持久补投 turn_end；正文到期不影响已确认 ready 的收轮。仍未知且无成功开轮证据时不盲发、不假装取消、不重开旧轮。完整规则见 TURN_END_RECOVERY_SPEC.md，App 回执见 APP_交接回复.md 顶部。

此补投仅补生成终态和真实回复编号映射，不重新注入召回、不增加 Heart 刺激、不自动创建长期 Event；不改变耳朵边界。完成活动水位仍由 Heart/App 单独确认，本回执不替 Heart 定 completed_activity_revision。

## Heart 接收与 app 接线状态（2026-10-21）

已读 `D:/your-heart/HEART_对Ember回复.md` 顶部集中反馈。Heart 接受本次快照交付，未发现新增 Heart 接口阻塞项，不需要 Ember 新增推送或修改快照协议。用户转达 app 已开始本地接线，其完成情况以随后接线反馈为准；不再将“app 休息、等待快照接口”作为当前阻塞。

接线需遵守 Heart 的集中约束：app 将 snapshot.recall_refs 转换成 Heart 允许的字段，不透传查询外壳、turn_messages 或 source_evidence；原始身份/版本缺失时只从持久原请求账恢复，不填当前值；sources_revision、resolution_revision 和窗口/活动版本各自独立；初始登记持久确认后才发独立解析更新。202/unknown/unavailable 不伪报空召回，410 仍可查询 refs，恢复不重开旧轮或训练耳朵。

本次只同步交接文档，没有修改业务代码、重跑测试或部署。191 项仍是上一轮 Ember 本地验证结果，不是 Heart 重跑或三方验收。主动 run、专用证据、原文客户端迁移和元数据删除生命周期仍为后续独立事项；调度保持关闭。

## 召回快照交付（2026-10-20）

用户已确认临时召回正文保留 24 小时，本地已实现稳定 recall_id 的原响应重放、GET /ember/recalls/{recall_id}、并发 pending、事务提交与记账恢复、到期正文清理及保留防复用身份。默认 refs 视图保留不可变呈现与引用，response 视图在 24 小时内返回原响应；读取和重试不续期、不开轮、不恢复旧写入权限。到期 response 返回 410 response_expired，refs 仍可读，不删除原有记忆或 app 聊天。

接口、故障状态和留存细节见 RECALL_SNAPSHOT_SPEC.md；app 接线见 APP_交接回复.md。sources_revision 固定 0，source_candidates 是冻结事件引用的副本，没有可信证据自动补交，不把后来回复回填成旧证据。迟到解析仍由 app 唯一触发，初始 Heart 登记确认后才能另投 resolution；不重放普通刺激、不复活旧周期、不训练耳朵。

累计 191 项 unittest 通过，未提交/推送/部署，未做真实三方联调。app 尚需补普通 MCP 两个身份头、turnEnd.recall_id 和 pending/过期分支。主动 run、工具结果证据、显式 pending 核验及新原文客户端仍待做；没有新增每次唤醒自动写记忆。

## 本轮局部实施状态（2026-10-20）

以下原始核对与方案保留为 a5a7c8f 时点记录；本轮已按用户授权局部实现最终呈现元数据、出处辅助和新实时事件来源账，具体字段与限制以本仓库 RECALL_REFS_SPEC.md 为准。未提交、未推送、未部署；不等于完整第二阶段完成。

轮前响应已新增 presentations/recall_refs；来源账仅保存写入时定位语料里明确绑定的 seq，不从旧最大 reply 回填，也不在后来收轮时替换原证据。缺来源保持 unresolved。旧 `/api/source` 和选材、门槛、注入文本不变。

此处是呈现/出处段的阶段记录。普通生成绑定及稳定召回快照已在后续本地段完成，见顶部最新交付、GENERATION_BINDING_SPEC.md 和 RECALL_SNAPSHOT_SPEC.md。run/pending 完整证据链仍未实现，没有新增唤醒自动写记忆。

本段验证：136 项 unittest（新增 21 项）通过；独立验收脚本全部成功退出，其中两项真实数据用例因缺 data_real 跳过。与 a5a7c8f 原渲染函数比较 19 种场景，注入文本逐字一致。本段只做本地隔离验证，不冒充三方联调。

## 普通生成绑定的本轮交付（2026-10-20）

已本地实现 X-Ember-Recall-Id / X-Ember-Tool-Call-Id 逐请求绑定；带 recall_id 的 turn_start/turn_end、生成和工具回执查询；同实例同生成同 tool_call_id 的结果防重；已结束/替代生成拒绝新的迟到写入，完成的旧调用可读原回执。未知副作用只记 unknown，不自动重跑。来源账区分 writer_recall_id 与历史来源回复的 source_recall_id，不用创建生成冒充整段出处。

新接口和 app 接线要求见 GENERATION_BINDING_SPEC.md。本轮只改 Ember，app 的 turnEnd 仍需带 recall_id；普通 MCP 同时补身份头后才能接真实新模式。未提交/推送/部署，未做真实三方联调。

本段验证：166 项 unittest 通过（本段新增 30 项，含实际 ASGI/MCP 接线）；独立验收全部成功退出，两项真实数据用例缺数据跳过。

明确边界：后续快照段已支持 24 小时原响应重放；旧版只有生成登记的记录仍返回 409 recall_snapshot_unavailable。read_memory_source 回执原文仍只在进程内缓存，重启后 result_not_retained；写入类及 search 完成回执持久保存。查询返回已知状态，不宣称所有工具跨 JSONL/SQLite 严格 exactly-once。主动 run 头当前拒绝，不降级到普通槽。

## app 休息前交接的本轮回复（2026-10-20）

已只读核对 app 新增的 recall.ts、revision.ts 和当前 ember.ts/mcp.ts；文档 §5 尚未同步这两段实现。Ember 对应的头名、快照查询提案、迟到解析唯一触发方和 run/tools 字段提案已写入本仓库 APP_交接回复.md。

两个普通头和 GET /ember/recalls/{recall_id} 已在本地实现；元数据/完整响应视图分开，正文 TTL 已由用户确认 24 小时，来源辅助目前固定不自动补证据。迟到解析由 app 唯一负责，初始登记成功后才发独立 resolution。主动 run 命名可供 stub，但业务实现及专用证据仍待做；未部署。

五项窗口版本细节属于 Heart/app 接收水位和生命周期，需要 Heart 自己确认；Ember 不代替接受。特别注意开始版本的 end 与结束活动递增、pending leaf 不在 done 数组、window delivery 的时序规则。

## 1. 结论与共同边界

1. 自动召回的筛选、语义门槛、席位、轻量续注和选材逻辑保持现状。不在空闲时额外选材，不因为引用无法解析而放宽召回。
2. 现有轮前接口没有完整、结构化的事件出处输出；内部具备事件 ID、来源会话及 Ember turn 区间，但不能保证按 Deliz 原文路径展开出有序 seq。需要补结构化引用与路径解析适配。
3. 现有接口不支持独立于真实用户输入的主动执行生命周期，也不支持可靠记录工具行动和安静终态。建议新增独立 run 入口，不用假用户消息凑一对，不借用正常聊天 turn 计数。
4. 建议明确由 app 唯一转发实际自动召回引用给 Heart；Ember 不再另推一份。Heart 查自己的真实回响，Ember 不生成 echo、情绪强度或 ODE 评分。
5. 临时唤醒提醒和后续请求层来源注记都不进入 Ember 聊天语料、原文引用或用户消息计数，不触发普通 turn_start 或自动召回。用明确 kind/run 身份区分，不能靠扫描文本标签识别。
6. 整个唤醒链路不训练耳朵，包括近期回放、旧记忆回放、sleep 回响、主动轮 valid/null 回响及其遗忘/习惯更新。Ember 没有 Heart 的学习入口，不制造新普通对话回响；app 和 Heart 必须按已登记的 cycle/sleep/run 来源路由到不学习的路径，不能仅靠自由填写一个开关。正常真实对话首次回响的学习规则保留。

## 2. 自动召回原文引用：当前实际有什么

### 2.1 对外输出与内部信息要分开

现有 `POST /ember/hooks/turn_start` 返回：

```json
{
  "injected": true,
  "injected_text": "[记忆浮现]…",
  "session_id": "当前触发会话",
  "turn": 7,
  "memory": {
    "recalled_count": 2,
    "entity_recalled": 1,
    "entity_names": ["…"],
    "filtered_by_sleep": 0,
    "filtered_by_window": 0,
    "resolved_brought": [],
    "direction_focused": null,
    "direction_reason": null,
    "summary_fallback": false,
    "semantic_matched": 1,
    "semantic_reason": "ok"
  }
}
```

这是结构示例，不是新增契约：

- 顶层 `session_id` 是触发召回的会话，不是每条旧事件的来源会话；`turn` 是 Ember 的会话内整数轮号，不是 Heart turn_id 或 Deliz seq。
- 注入文本可能出现 `EVT-…` 稳定事件 ID、线索和内容；轻量实体块也可能只呈现实体名与经历数量。不能靠解析文本可靠取得全量事件引用。
- `memory.resolved_brought` 只是被修正者引出的修正事件 ID 子集，不是全量召回列表；这里的 resolved 是关系修正语义，不是原文引用解析状态。
- 对外没有稳定 `recall_id`、全量结构化 `events`、`anchor_message_id`、每事件 `source_session_id`、原文 `message_ids`、来源 leaf 或引用解析状态。

内部已有的字段和账本：

| 对象 | 当前字段/结构 | 实际含义 |
|---|---|---|
| Event | `event_id`、`version_id` | 已持久化事件身份和内容版本；不同于协议操作幂等用的 event_id |
| Event | `source_ref=conversation/session-<sid>` | 来源会话；读取原文时解析此字段 |
| Event | `raw_ref=conversation/session-<sid>/turn-<n>`、`turn_refs` | 写入当时的出处锚点；不能替代经历真实范围 |
| turn_spans.jsonl | event_id → 多段 `[[turn_lo, turn_hi], …]` | 经引用句定位的 Ember 经历轮区间，可断开；未回填返回 `[]` |
| turn_ledger.jsonl | `(ember_session, ember_turn)` → `heart_turn_id / user_message_id / assistant_message_id` | 用户/回复 seq 与 Ember 轮的旁路映射，不含原文路径 |
| recall_ledger.jsonl | `session_id / turn / route / event_ids / cue / entity_id / direction / via` 等 | 内部召回记账，不是可供跨服务幂等转发的 recall 包 |

触发输入的 `user_message_id` 已可从钩子写入账本，但未作为结构化召回锚点返回。Cue 命中是“为何想起”的线索；真正的回放锚点应是此次触发的真实用户消息 seq，两者不能混用。

原文工具 `read_memory_source` 当前返回 `event_id / turns / transcript / context_expanded / truncated` 等；它不是引用解析接口，没有有序原文 seq 输出，单轮还会按阅读体验扩展前后轮。不能拿 transcript 或阅读扩展范围充当新契约的精确引用结果。

### 2.2 与 Deliz seq、leaf、路径的差距

按最新三方契约，Deliz 的 seq 全局唯一，JSON 用字符串传输；messages.id、leaf_id、context_start_id 是另一套 UUID，不是 seq。此为本轮契约已确认的 app 事实，本次未独立审计 Deliz 数据库。

Ember HTTP `_opt_int` 接受数字字符串并转成 Python int，所以真实 seq 的数值可以接入现有 user/reply 映射；但这不等于已经兼容新版引用契约：

- 当前 source 客户端只接受整数 `message_id`，请求 `/api/source?session_id=…&from=…&to=…`，没有 leaf、显式有序 ID 或路径上下文。
- `TurnLedger.message_span()` 取各轮已知 user/reply ID 的数值最小/最大值；`turn_of_message()` 用 `[user, reply]` 数值范围推断归属。
- 同一 Ember turn 的多个 reply 映射只保留最大 reply ID；现有钩子也没有 Heart 新版“同一 start、一对多回复”的稳定幂等绑定。重生成后不能据此还原所有历史回复的路径选择。
- 当前没有 branch/leaf 记录；同 session 的内存语料也没有按 Deliz 有效路径隔离。仅凭 turn 区间不能证明属于哪条保留路径。
- 旧钩子的 `context_start_id` 是数字 seq 口径；新 Heart window 的同名字段是 UUID。不能把新版 UUID 直接塞给旧字段，否则会被解析成缺省。新版应分清 app 不透明 UUID 和有序 seq 列表，保留旧接口原语义。

因此回答是：**现有映射可作辅助，但现在不能保证展开成正确路径上的有序全局 seq。不能直接把数值区间交给 Heart。** 消息 ID 即使全局唯一，也不能用大小关系证明 parent 路径。

### 2.3 建议的最小召回引用扩展（待三方确认）

1. 在现有真实对话自动召回响应上增加独立引用块，不更改现有注入文本或筛选规则。实例仍取 `X-Heart-Instance`。
2. 引用块包含稳定 `recall_id`、触发 `session_id`、`anchor_message_id`（真实 seq 字符串）、app 提供的 activity/window revision，以及实际呈现单元关联的事件 ID。
3. 由最终呈现过程产出元数据，不把预筛选候选、被名额淘汰者、强落选检索提示或全库成员当作已经呈现。轻量实体概览需标注呈现粒度，不能把内部成员列表默认为每条都已展开；具体回放资格由三方确认，不悄悄扩选材。
4. 每条事件给出 `event_id`、`source_session_id`、已知 Ember turn spans，以及可验证的映射辅助；以后新写入时同步保存来源路径不透明引用/有序来源 seq，放在旁路出处账本。branch_id/source_branch_id 可省略，不伪造 main。
5. app 持有 parent_id 原文树，负责按事件来源的真实路径展开各段有效有序 seq；不把来源强行限制为当前 leaf，历史保留路径仍可作为当前真实召回的来源。若来源路径不唯一且不能验证，标 unresolved。Ember 不读取 Heart session 文件，也不自行按 seq 排序或补齐数值孔洞。
6. app 将完成解析的结果唯一转发给 Heart `/api/wake/v1/recalls`；空召回为 `events: []`，一天无召回也正常。主动 search 或 Heart 的回放本身不冒充普通自动召回。

建议的 Ember → app 单条未解析引用形状（仅提案；不是当前返回值，也不是已经定稿的 Heart 请求 schema）：

```json
{
  "event_id": "EVT-example",
  "source_session_id": "s0",
  "source_turn_spans": [[3, 4], [7, 7]],
  "resolution": "unresolved",
  "reason": "source_path_unknown",
  "message_ids": null
}
```

映射缺失时保留已知的稳定事件身份，未知 source_session_id 也用 null，不填当前会话冒充；`message_ids: null` 表示未知，不能用空数组谎报已解析。缺 span、缺 seq、来源路径不明确、来源服务不可达分别用稳定原因区分。原文没有映射不等于事件没有发生，也不应使正常对话失败。

解析成功后给出 `resolution: resolved` 与真实有序 `message_ids: ["71","72","75","76"]`；只有实际永久删除/撤销才标 invalidated。普通截断、切分支、重生成不永久作废历史；当前回放池按当前窗口锚点和版本移除或重绑，不倒放身体、不重播历史回响。

稳定身份与迟到映射还需补一个明确协议：建议 app 提供稳定的本次召回操作 ID，Ember 持久保存该次实际呈现的引用快照，重试返回原快照，不重新召回/记账。同一事件后续真实召回可属于新的 recall_id。unresolved 后续补齐不重新跑召回；建议用独立解析更新身份和版本关联原 recall_id，避免同一幂等 event_id 内容变化触发 id_conflict。现接受 v0.5 第 6.2 节的独立 resolution 更新方向（详见下节），仍待 app 确认；解析补齐本身不立刻回投 ODE。


### 2.4 对 Heart v0.5 的确认：呈现层可以稳定区分，尚未输出结构化分类

答复：**可以在最终渲染分支同时产出明确的呈现类型，无须改门槛、席位或显示文案；现有代码尚未返回这些元数据。** 不能直接拿 selected_units、ctx.recalled_events 或 recall_ledger 当作呈现证明，因为它们可能含只呈现为实体概览的成员，记账也在渲染之前发生。

建议将“生成注入文本”和“生成实际呈现元数据”作为同一次呈现过程的两个产物，从实际写入 lines 的位置记录，而不是事后解析文本。分类以实际输出为准，同一单元也可以包含不同类型的条目：

| 当前真实渲染路径 | 建议 presentation | 可否成为事件回放候选 |
|---|---|---|
| `_event_line(eid)` 输出该事件 core（无 core 时 summary），含修正承接分支 | event_text；内部可记录实际内容层次 | 可以，须有稳定事件身份，原文和 Heart echo 仍另行解析 |
| 事件行附带实际 key_quotes 或成功内联原文 | 作为该事件的文本/摘录呈现信息；同一事件去重，不因多段内容制造多次接触 | 可以；不重复计入同一事件 |
| summary_fallback 中确实单独输出某事件 summary 与 ID，而非隐藏成员清单 | event_excerpt | 可以，只引用确实输出该摘要的事件 |
| `_light_line` 的散事件路径，确实输出事件自己的 summary | event_excerpt，并保留 light 呈现属性 | 可以；不因此重新全文展开，Heart 仍执行去重/冷却/衰减 |
| observe 附加席实际输出信息点 summary 与 ID | event_excerpt | 可以进入出处解析；origin=observe 不保证必有 Heart echo，缺 echo 保持 unresolved，不制造参数 |
| entity_block 只输出实体 summary 的部分；summary_fallback 的实体说明和名下 ID 清单 | entity_overview | 不把清单成员自动当作逐条展开 |
| 轻量 entity_block 只报实体名与 N 条经历；`_light_line` 只报实体名下相关经历与 ID，未输出该事件内容 | entity_overview | 不因内部成员或可见 ID 获得逐事件资格 |
| strong_dropped 尾部“另有 N 条未展示”以及仅用于查找的工具指引 | retrieval_hint | 无；不补出落选事件或隐藏成员 |

实体块不能一概视作概览：正常 entity_block 会先输出实体 summary，再逐个调用 `_event_line`，这些真正输出的成员应各记 event_text；只有概览的分支才排除隐藏成员。summary_fallback 也不能一概视作全文展开，要按每条实际输出分类。修正承接里对旧认知的背景说明不自动把旧事件加成新的独立召回条目，仍以真正被呈现的修正事件为准。

建议快照的 `presentations`（内部/Ember→app 字段名提案）保留有序呈现信息，允许记录概览或提示；`recall_refs.events` 仅收 event_text/event_excerpt 且具备稳定事件身份的实际内容条目，附 presentation、原值 version_id、出处或 unresolved。概览记录可以有 entity_id，但不得携带一组伪装成已展开事件的成员。整次只有概览时，事件回放引用可以是 events=[]，这不等于本次没有显示概览；用 presentations 保留该区别。

不将“正文展示”说成“原文展示”：event_text 可能是记忆 core，而不是聊天原文；event_excerpt 也可能是事件摘要。presentation 只是接触内容粒度，resolution 只表示原文路径，echo_resolution 只表示 Heart 自报是否可用，三者互不代替。接受 v0.5 首版只让前两种取得候选资格的方向，不要求 Heart 立即回投，最终字段待 app 确认。

### 2.5 对稳定 recall_id 快照的确认：可实现，不能直接复用旧账本

答复：**可以按 `(instance, recall_id)` 持久保存并在重试时返回同一份快照；目前没有该能力，需要接口定稿后实现专门的旁路快照存储。** 现有 recall_ledger 是分通路、多行、允许旁路失败的统计账，不存注入原文和完整身份，不能承担跨重启的幂等权威。

建议的请求/快照约束：

- app 在第一次请求 Ember 前持久分配 recall_id，绑定本次真实生成/回复；重试保留同 ID、真实用户锚点、session、原始发生时间及活动/窗口版本。真重生成使用新 recall_id，可生成新快照；这不要求 Heart 重新施加同一用户刺激，不能把 Ember 轮号混成 Heart turn_id。
- 快照保存实际返回的 injected/injected_text、Ember session/turn、原始 memory 统计、有序 presentations、recall_refs，以及业务请求指纹和格式版本。应缓存最终响应数据，不在重试时按当前模型、时钟、pin 状态、事件内容或向量库重新渲染。
- 保存实际呈现时的事件 version_id，缺失为 null；不临时制造新版。现有 revise 会原位改内容而不保证 version_id 随之变化，因此仅保存 EVT/version_id 不足以重现历史：必须固定当时文本、呈现粒度和出处辅助的快照，不从现有 Event 重建旧结果。
- 快照可以存已经实际注入的截断文本，但不额外拉取完整原文或复制 app 消息库。当前高匹配可能内联有限原文，持久保存 injected_text 会保留这部分已经注入的片段；这是相对现行“按需读取、用完即弃”的新增留存，需在接口/保留策略中显式确认，不能隐藏这一变化，也不能靠重拉原文声称返回同一快照。
- 同 ID 同业务内容：返回原始响应，不再次 embedding、选材、渲染、开普通 turn、累加召回记账或刷新在场/遗忘计时。delivery 等可变投递元数据不计入业务内容；session、锚点、生成身份、版本及原始发生时间等语义字段不得换值。
- 同 ID 不同业务内容：409 id_conflict，不以“当前窗口变了”为由覆盖原快照。当前模型请求要使用新窗口/重新生成时，应由 app 建立新的生成/recall 身份；普通网络重试不能偷偷升级请求内容。
- 空召回和只有概览的结果也保存；原始 unresolved 状态也冻结在初始快照，不因后续映射补齐而改写同一幂等响应。解析更新另走独立操作。
- 重取旧快照不恢复旧 `_live_turn`，不重开写入槽，不使旧轮覆盖当前轮。取回响应只证明历史结果，不授权在当前工具循环使用其旧 turn；app 必须按本次生成/活动版本防止过时注入和工具写入。新一轮的普通写入身份还需在钩子契约中明确。

持久化与并发边界不能只加一个缓存字典：建议同实例同 recall_id 的并发请求只有一个创建者，其他请求读取已完成结果或取得明确 in_progress 状态；已完成记录跨重启保留。新路径在开轮前先做幂等检查和持久占位，选择、渲染、出处及响应固定后原子提交快照/轮身份和可恢复的记账操作。旧统计账需以该操作身份幂等应用，或由提交记录重建，避免崩溃重试重复计数。不能把“先执行旧 hook，再写缓存文件”当作完整实现。

故障时宁可返回明确 pending/in_progress 或不可用，并允许用同 ID 查询/恢复；未完成结果不报持久成功、不由第二个请求盲目再开一轮。恢复只继续已记录的操作/返回已提交快照，不悄悄用同 recall_id 跑出不同选材。app 仍可降级继续普通聊天，但不能把无快照的结果当成已确认回放引用。具体存储实现和 HTTP pending/查询路径留待接口定稿，不在本轮新增业务代码。

快照保留期/清理规则也应列入对接：不能清掉正文后仍把重试当新召回来跑；若以后允许清理，至少保留幂等身份/墓碑并返回明确不可恢复状态，不能重用 recall_id。

### 2.6 解析更新和主动 pending：本轮接受的明确范围

接受 v0.5 的 `POST /api/wake/v1/recalls/{recall_id}/resolution` 方向：app 唯一发送，新操作 event_id、expected_resolution_revision 与递增 resolution_revision；原初始包不改。只更新已登记事件及当时内容版本的出处状态，不新增未呈现成员、不把后来新版塞回旧 recall；旧 revision 报 stale_resolution、同操作异内容报 id_conflict。原始内容版本不足以验证时维持 unresolved，不用现存新版重新解释旧快照。更新不立即刺激、不清零、不取消或复活旧周期。Ember 初始快照、app 的解析更新账和 Heart 的 echo_resolution 分别保留职责。

再次确认：**不新增每次唤醒自动写记忆。** run start/finish、安静终态、真实主动消息或工具结果存在，都不会自动调用记忆写入工具，不自动生成 Event，也不新增后台 writer 审查。pending 仅用于兼容 AI 在主动轮中已经显式发出的记忆工具调用，且其引用的本轮真实回复尚未提交这一情况；没有显式调用就没有待写记忆。具备可验证历史证据的显式调用可以走相应验证路径，不能一律改成 pending。pending 以稳定 tool_call_id/run_id 记账，未核验不报 created；取消/失败时保留实际工具副作用账，不把未提交正文当真实经历。专用工具证据和 run 原文读取仍须先实现并验收，不能先开放工具再以 pending 永久搁置掩盖不支持。

待 app 确认仅保留直接对接项：稳定生成/recall 身份如何传入及如何重试；原文路径解析/来源证据格式；MCP 能否按请求携带 run_id 与稳定 tool_call_id；真实回复提交与显式 pending 工具核验/finish 的顺序。Ember 这边已确认呈现分级和稳定快照的可行性；字段、状态及保留策略定稿后再实现。

## 3. 主动行为入档：现状与最小扩展

### 3.1 现有接口不能直接承担

- HTTP turn_start 要求非空 `message`，会增加正常 Ember turn、调用召回并把内容作为 user 加入会话。
- turn_end 必须存在 `_live_turn`；若给 turn，则与当前号核对。技术上 turn 参数可省略，但并不因此支持无 start 的主动结束。
- 每个 EmberService 有一个共享 `_live_turn`，不是按 run_id 的持久执行上下文。
- MCP create_memory_event 也依赖此共享槽；事件使用正常 conversation/session/turn 出处。无真实用户输入时没有合法主动写入上下文。
- Event 已有 origin 字段，但当前正常写入不设置 assistant_initiated，没有 run 关联账本；仅有此字段不等于已支持主动行为。
- 没有主动 run 的工具行动账本、无正文完成、取消/失败终态和跨重启幂等查询入口。旧 turn_end 传空回复会添加空 assistant 记录，不能拿来模拟安静结束。

### 3.2 建议新增独立 run 生命周期

为保持旧钩子兼容，建议新增以下 Ember 接口（路径为本次提案，与 Heart `/api/wake/v1/runs/start` 不是同一个服务）：

| 提议入口 | 最小职责 |
|---|---|
| `POST /ember/runs/start` | 登记 `kind: wake`、`origin: assistant_initiated`、session_id、wake_id、run_id、原始 occurred_at、活动/窗口版本及实际窗口/leaf 引用；不收临时提醒正文，不增加正常 turn，不做自动召回 |
| `POST /ember/runs/finish` | 按 run_id 登记 done/cancelled/error、spoke、真实 reply_message_id 或 null、真实工具行动及结果引用、finished_at；没有用户则 user_message_id=null，不创建 user 或空 assistant 消息 |
| `GET /ember/runs/{run_id}` | 查询持久终态、关联事件及工具结果，供超时重试/重启对账 |

start/finish 分别带稳定操作 event_id；按 `(instance, event_id)` 幂等、按 `(instance, run_id)` 关联生命周期，相同身份不同业务内容返回 409 id_conflict。start 必须先持久接收；同一 run 的终态不能被迟到请求覆盖；finish 重试不重复写事件或执行工具。具体状态迁移和补账错误码在编码前与 app 定稿。

运行账是旁路工程记录，不能把 run_id 塞进正常 turn_refs，也不因每次结束就生成长期 Event。run_id 与正常 Heart turn_id/Ember turn 独立。

工具循环期间还需要显式绑定上下文：建议 app 每次 MCP 请求携带 `X-Ember-Run-Id`（提案），HTTP 层与实例头共同校验并绑定到该请求；Ember 根据持久 run 记录取得 session/路径。请求指定 run 时必须解析该 run，未知、已结束或不匹配则报错，禁止回退到共享 `_live_turn`。不要求模型自行填写执行身份，也不将运行 ID 放进提醒文本让模型猜。

工具行动由 app 记录稳定 tool_call_id、实际完成/失败/未知状态及必要的结果引用，再关联 run；未知结果需对账，不能盲重放有副作用的工具。工具已执行但随后 run 取消时，副作用仍照实保留，不假装回滚。此记录不要求 Ember 执行或复制管理其他系统的工具。

### 3.3 实际回复、工具行动与长期 Event 的分界

- 主动有声：app 先保存真实 assistant 消息，再以其全局 seq 关联 run；可从原文持有方核对内容。没有对应 user，不补造 user，也不额外计一次普通对话。
- 安静但有工具行动：finish 可记录 spoke=false、reply_message_id=null 和真实工具结果。是否值得成为长期经历仍按现有记忆写入规则判断，不要求造正文。
- 完全安静：finish 仍记录 done；不生成叙事 Event、不创建空气泡。
- 主动长期记忆写入仍应是显式行为，不让每个 run 自动转成 Event；需要记录时设置 origin=assistant_initiated，以旁路出处账记录 run_id 和真实来源引用。
- 现有 quote_spans 要逐字核对聊天语料，不能直接用于没有真实文本来源的工具行动。不能为适配主动轮降低核验标准，也不能拿临时 reminder 充当证据。
- 建议 run 内写入请求显式绑定 run：引用已有真实历史时按该 run 已验证路径核对；若引用尚未落库的本次主动回复，仅对这次显式工具调用持久登记待提交写入意图，finish 后用真实回复 seq 核验再落 Event；没有显式调用不建立 pending。回执必须明确 pending/created/failed，不能未落库就报 created。
- 只有工具结果的经历若需入长期记忆，应使用可验证的 tool_call_id/结果引用作为专用证据来源；此类 schema 需另行明确，现有只接受 user/ai quote 的 schema 不宣称已支持。第一步可先可靠记 run/tool 账，不强制生成长期事件。
- 主动 Event 的原文读取/检索出处也需认识 run 来源，不再只认 conversation/session/turn；旧事件读取路径保持兼容，不能给新主动事件虚构正常 turn 来骗过读取逻辑。

Ember 的 finish 只负责执行/记忆对账，不负责向 Heart 再转发一份 echo。app 仍按主契约 `turn-end(kind=wake)` 唯一投递真实回响；sleep 按 sleep/complete 唯一投递，避免双投及误入普通学习链。

## 4. Ember 工具能力及副作用（给 app 制定权限）

| MCP 工具 | 现有副作用 |
|---|---|
| create_memory_event | 写 Event、召回 Cue、出处 span、待判联队列等；不是幂等写入，重试可能生成重复事件；要求真实出处核验 |
| read_memory_source | 只读原文；可能按需访问配置的原文提供方；不将拉取原文持久落盘 |
| search_memory | 不是纯读：记录提取账、刷新遗忘计时；命中淡忘事件可恢复已收紧的 Cue；还可能调用 embedding 服务 |
| revise_memory_event | 冷静期内覆盖叙述字段；可整体重设 Cue、退役旧线索；不提供语义修订历史/撤销工具，出处和摘录锁定；定稿后拒绝直改 |
| pin_memory / unpin_memory | 改旁路钉住登记，幂等；影响简报及自动召回，不删除 Event；用户明确要求时才调用的语义保持 |

当前六个 MCP 工具没有通用删除事件工具。sleep link/structure 等另有写关系、归属、状态、向量、Cue 和观察事件的副作用，不应当作主动轮里的只读检索能力。主动工具权限由 app 制定，Heart 不维护另一份工具黑名单；不训练耳朵也不等于禁止 Ember 自己既有的提取记账。

## 5. 旧梦链路与接入次序

当前 Ember 代码和 API.md 已标明旧 dream_source/dream_push/夜账本链路于 2026-10-15 退役，未发现现行 Ember 向 Heart 推 `/api/dream/material` 的实现。此结论仅针对当前 Ember 仓库，不代表已审计所有 Heart/app 部署。Heart 关闭旧后台散文入口、app 切换新 sleep 完成投递时还需各自验收不能双跑；历史梦资料不由本轮删除。

建议先确认两份小契约：① 召回引用输出、app 路径解析和迟到映射更新；② run 生命周期、MCP 请求绑定及真实证据提交。随后实现并隔离联调，不先启用日常唤醒调度。无需重开已确认的全局 seq/无 branch_id 讨论，不把限额与 ODE 参数拉进 Ember 接口实现范围。

最低验收：空召回；实际呈现与内部候选区分；同 recall 重试；迟到解析；跨会话及多实例作用域；重生成兄弟节点和非连续 seq；切回保留 leaf；真正删除原文；旧无映射 Event；主动有声/工具独立/安静终态；取消后的工具副作用；run 重启与重试；临时提醒不入原文/不增加 turn/不触发自动召回；Heart 唤醒各路径 valid/null 都不学习，普通真实对话学习规则不变。

## 6. 本轮代码依据

- `sidecar.py`：`_opt_int`、`_hook_turn_start`、`_hook_turn_end`、MCP `_call_tool` 及实例路由。
- `ember/service.py:592`：召回流水线与返回统计；`1012`：呈现层的全文/概览/轻量差异。
- `ember/service.py:769`：写入与 quote_spans 核验；`910`：翻原文；`1291`：普通 Event 出处构造；`1318`：旧数值区间原文获取。
- `ember/service.py:1577`、`1615`、`1660`：普通开轮、收轮与依赖共享槽的 MCP 写入。
- `ember/turn_ledger.py:136`、`158`、`207`：回复映射、数值区间归属及 message_span。
- `ember/turn_span.py`：`fill / span_of / span_of_event`；`ember/source_client.py:46`：旧原文请求协议。
- `ember/service.py:1709`、`2312`、`3033`、`3046`：修订、检索、钉住及取消的副作用。
- `ember/models.py`：Event 的 origin 与出处字段；`ember/instances.py`：实例级存储隔离；`API.md`：当前服务契约及旧梦退役声明。
