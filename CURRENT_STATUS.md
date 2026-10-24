# Ember 当前状态

核对日期：2026-10-24（Asia/Taipei）。这份文件统一当前结论；其他交付文档保留各自日期和当时的验收边界，不能把旧的“未部署”当成今天的状态。

## 已部署与已验证

| 项目 | 当前证据与范围 |
|---|---|
| 日常 sidecar | 运行服务加载基线 `f910d4d`，`ember-sidecar.service` 运行中；离线工具/文档可通过后续提交同步 checkout，不需重启。部署前只读核对 PID 1785468，PID 仅为本次快照 |
| 旧模式历史摘录 | `89d41a4` 已随线上版本部署；精确路径核验、旧逐条编号兼容和错误分类见 [QUOTE_SOURCE_COMPAT_FIX.md](QUOTE_SOURCE_COMPAT_FIX.md) |
| 工具报错 | `53322ec`、`f910d4d` 已部署；线上 MCP 已核对错层级提示和业务失败的 `isError=true`，见 [TOOL_ERROR_RELIABILITY.md](TOOL_ERROR_RELIABILITY.md) |
| 部署前验收 | `f910d4d` 的 252 项 unittest、38 项 sidecar 验收通过；此前另有摘录 36、Phase 1 的 20、Phase 2 的 42 项独立验收通过 |
| 现有库只读体检 | `/opt/ember1/data`：8 事件、36 线索、4 冻结出处记录；所覆盖的 JSONL 结构和事件关联未发现错误。4 条旧事件缺冻结出处、1 个来源轮次缺完整编号单独统计；未补造来源 |

生产库未发现 `generations.sqlite3`；这与旧模式运行相符，不代表损坏，也不能据此声称普通生成身份绑定已在真实 App 链路启用。体检不连接 App/Heart，不验证聊天原文真实性或远端授权。

## 本次本地交付

新增 `ember/health_check.py`、`scripts/check_memory_health.py` 和回归测试。工具只读原目录；SQLite 复制 DB 与 WAL 到临时目录后核验，不运行 Store/Service 的恢复逻辑。遇到检测期间数据变化返回 `inconclusive`。使用说明见 [MEMORY_HEALTH_CHECK.md](MEMORY_HEALTH_CHECK.md)。

本次新增体检专项 15/15 通过；最终源码在 VPS 临时目录完整运行 267/267 unittest、38+36+20+42 共 136 项独立验收全部通过。所有测试使用临时数据和模拟提供方。

体检工具先通过临时包完成线上库只读检查，随后按用户授权同步至 VPS checkout。它是手动离线入口，无需重启或改变 sidecar 服务；部署后用 CLI 验证，运行服务继续保持原 PID。

## 尚不能宣称完成的事项

| 事项 | 负责边界与下一步 |
|---|---|
| 第二阶段真实三方生产联调 | Ember 的普通生成身份、回执、快照和来源补交代码及隔离验收已有交付；App/Heart 实际接线、生产配置与组合拒错仍需共同核验。代码已部署不等于链路已启用 |
| 原文提供方生产权限与令牌 | 2026-10-21 的隔离实例归属与专用令牌验收有效；不能替代当前生产令牌切换、通用令牌轮换、重启后配置与授权拒错复验。本次不读取令牌、不改配置 |
| 主动 run / 唤醒 | 主动 run 及专用证据链仍待实现与验收；本次不改变任何开关，也不新增自动写记忆 |
| 完整异常组合 | TTL 到期、取消/失败、跨实例/会话/版本等已有局部回归；不能把隔离测试汇总成真实三方全部完成 |

来源与历史边界见 [PRODUCTION_PREREQUISITES.md](PRODUCTION_PREREQUISITES.md)、[SOURCE_PROVIDER_PROCESS_VALIDATION.md](SOURCE_PROVIDER_PROCESS_VALIDATION.md)、[PHASE2_INTEGRATION.md](PHASE2_INTEGRATION.md)。此前 #2 精确路径迁移和 #5a 四个进程强杀检查点已完成，不再列为未做；#3 的生产范围与功能开启验收继续单列。
