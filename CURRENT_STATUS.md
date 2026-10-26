# Ember 当前状态

2026-10-25 补充：quote_spans 出处定位已增加格式容错并按用户授权部署 VPS `/opt/ember1`，服务重启成功，实际 MCP 工具描述已核对。精确子串优先；未命中时仅按文字/数字连续匹配，忽略 emoji、Markdown、标点及空白，唯一消息候选才接受。多候选返回 quote_ambiguous、具体端点字段及候选数，落盘前拒绝。key_quotes 原话校验不随之放宽。VPS 临时副本完整 unittest 302/302、turn_span 8 项、sidecar 38 项通过。仅替换 turn_span.py、source_paths.py、writer.py，保留线上已有 run 改动和配置；部署记录见 QUOTE_FORMAT_DEPLOYMENT.md。

核对日期：2026-10-24（Asia/Taipei）。这份文件统一当前结论；其他交付文档保留各自日期和当时的验收边界，不能把旧的“未部署”当成今天的状态。

## 已部署与已验证

| 项目 | 当前证据与范围 |
|---|---|
| 日常 sidecar | 运行版本 `0d5c1f7`，`ember-sidecar.service` active，启用后只读确认 PID 1855103，配置文件权限 0600。离线工具 `054821f` 已同步，日期标注 `0d5c1f7` 已部署；PID 仅为本次快照 |
| 旧模式历史摘录 | `89d41a4` 已随线上版本部署；精确路径核验、旧逐条编号兼容和错误分类见 [QUOTE_SOURCE_COMPAT_FIX.md](QUOTE_SOURCE_COMPAT_FIX.md) |
| 工具报错 | `53322ec`、`f910d4d` 已部署；线上 MCP 已核对错层级提示和业务失败的 `isError=true`，见 [TOOL_ERROR_RELIABILITY.md](TOOL_ERROR_RELIABILITY.md) |
| 部署前验收 | `f910d4d` 的 252 项 unittest、38 项 sidecar 验收通过；此前另有摘录 36、Phase 1 的 20、Phase 2 的 42 项独立验收通过 |
| 现有库只读体检 | `/opt/ember1/data`：8 事件、36 线索、4 冻结出处记录；所覆盖的 JSONL 结构和事件关联未发现错误。4 条旧事件缺冻结出处、1 个来源轮次缺完整编号单独统计；未补造来源 |

此前只读体检时未发现 `generations.sqlite3`，对应当时的旧模式；这不是损坏，也不应覆盖下方随后完成的日常第二阶段配置启用。新生成账与完成水位需在启用后的真实对话结束后核验。体检不连接 App/Heart，不验证聊天原文真实性或远端授权。

## 日常第二阶段配置启用已接收（2026-10-24 16:49 回执）

来源：App 的 `D:/deliz/PHASE2_ACTIVATION_2026-10-24.md`。本方阅读回执后仅独立核对 VPS 版本、服务 PID/状态、`.env` 权限；下列接口/权限与 Heart 数值为 App 本轮实测记录，不冒称本方再次执行。

- Deliz 已配置独立 `DELIZ_SOURCE_TOKEN`，VPS Ember 已切换 `EMBER_SOURCE_TOKEN` 并重启；手机使用的通用客户端令牌未更换。
- 独立令牌读原文 200、通用令牌读原文 401、独立令牌访问会话接口 401、通用令牌访问会话接口 200；真实 Ember MCP `read_memory_source` 返回 `status=ok`。
- 缺/未知实例 403、缺失会话 404、混合兄弟分支 409。日常库没有另一个已知实例的会话，跨已知实例拒绝仍引用之前隔离验收，不声称本轮做过日常跨实例案例。
- App 已开启 `EMBER_RECALL_REFS=1`、`HEART_PHASE2=1`，Heart 已收到真实家窗口：session main，window revision 9，activity revision 117，30 条真实消息编号；`window-main-9` outbox sent。
- 启用回执时没有新真实生成，recalls 为空，`known_completed_activity_revision=null` 是未取得新完成证据，不能改成 0 或视为故障。

下一步由 App 正常对话触发，不造测试聊天或回响：对同一实际 recall/generation 身份核对 Deliz recall/outbox、Ember snapshot/turn_end、Heart recalls/end 与完成水位。无记忆命中时空引用可合法；无真实完成证据不能宣称水位已推进。主动唤醒仍 `wake_enabled=false`，新版 sleep/主动 run 不在本次配置交付范围。

## 本次本地交付

新增 `ember/health_check.py`、`scripts/check_memory_health.py` 和回归测试。工具只读原目录；SQLite 复制 DB 与 WAL 到临时目录后核验，不运行 Store/Service 的恢复逻辑。遇到检测期间数据变化返回 `inconclusive`。使用说明见 [MEMORY_HEALTH_CHECK.md](MEMORY_HEALTH_CHECK.md)。

本次新增体检专项 15/15 通过；最终源码在 VPS 临时目录完整运行 267/267 unittest、38+36+20+42 共 136 项独立验收全部通过。所有测试使用临时数据和模拟提供方。

体检工具先通过临时包完成线上库只读检查，随后按用户授权同步至 VPS checkout。它是手动离线入口，无需重启或改变 sidecar 服务；部署后用 CLI 验证，运行服务继续保持原 PID。

## 尚不能宣称完成的事项

| 事项 | 负责边界与下一步 |
|---|---|
| 第二阶段真实对话验收 | 日常配置已启用、当前窗口投递已验证；启用后的新真实生成尚未发生，召回引用、turn_end 和完成水位留待下一轮正常聊天核对 |
| 原文提供方 | 日常独立令牌切换、重启、真实 MCP 原文成功及本轮权限/归属拒错已由 App 16:49 回执验证；日常跨已知实例案例未新增。通用客户端令牌保留，不把旧令牌轮换扩大解释为已完成 |
| 主动 run / 唤醒 | 首版独立 run 生命周期、MCP 只读绑定已在本地工作区实现并隔离验收，见 RUNS_SPEC.md / APP_WAKE_RUNS_REPLY.md；尚未部署日常。主动写记忆与专用工具结果证据仍不开放，主动发送未开启 |
| 完整异常组合 | TTL 到期、取消/失败、跨实例/会话/版本等已有局部回归；不能把隔离测试汇总成真实三方全部完成 |

来源与历史边界见 [PRODUCTION_PREREQUISITES.md](PRODUCTION_PREREQUISITES.md)、[SOURCE_PROVIDER_PROCESS_VALIDATION.md](SOURCE_PROVIDER_PROCESS_VALIDATION.md)、[PHASE2_INTEGRATION.md](PHASE2_INTEGRATION.md)。此前 #2 精确路径迁移和 #5a 四个进程强杀检查点已完成，不再列为未做；#3 日常原文权限切换和功能配置启用已按上述回执更新；首轮真实生成与剩余异常组合继续单列。

## App wake-integration 首版接入（本地，未部署）

已补 start/finish/query 和独立 run/tool 身份、固定完成意图及恢复对账。完整 293 项回归、38 项旧 sidecar、新增 run 24 项专项通过。App 已对齐只读白名单及 503 + finishing 的同身份补投，随后真实 App、Heart、Ember 隔离 HTTP 联调 20/20 通过：quiet/visible、真实 MCP 绑定、503 后双端重启补投、App 流中断恢复和窗口活动取消。详见 [WAKE_THREE_PARTY_VALIDATION.md](WAKE_THREE_PARTY_VALIDATION.md)。模型和候选触发由测试夹具控制；未部署 8790 或日常 Ember runs，主动发送未开启，Heart 自身重启和真实调度/模型行为不在本轮范围。
