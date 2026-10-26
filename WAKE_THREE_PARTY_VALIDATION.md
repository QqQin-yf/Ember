# 隔离真实三方联调回执

2026-10-24，Asia/Taipei。**真实 App、Heart、Ember 三个独立服务进程的 HTTP 联调 20/20 通过，进程退出码 0。日常主动发送未开启，未部署或重启日常服务。**

源码基线：Ember `0d5c1f7` 加当前未提交的 runs 实现；App `D:/deliz/.claude/worktrees/wake-integration`，HEAD `3eba766` 加该 worktree 当前改动；Heart `D:/your-heart`，HEAD `5c0c47e` 加该工作区当前内容。HEAD 只标明基线，不能据此忽略未提交实现。

## 本轮实际连接

- App：真实 `server/src/index.ts` 子进程、临时 SQLite，原来的 Generation 和 MCP 客户端。
- Heart：真实 HeartServer HTTP handlers、候选周期、runs/start、turn-end、窗口和持久事务。仅候选触发时机由夹具用合成窗口和虚拟时间准备，不启动自主心跳或投递线程。
- Ember：真实 sidecar 子进程，独立 token、实例和临时数据；真实 `/ember/runs/*` 与 MCP。可见 finish 经真实 App 原文 API 核验。
- 模型：本机确定性 Anthropic SSE 替身；原文前有专用故障网关，仅在指定阶段返回 503，其余请求转给真实 App。

所有端口由系统分配并只绑定 loopback；三方使用临时合成历史，不读取日常库或 `.env`。临时凭据不进入源码和回执，退出时清理临时库、配置和测试进程。未改 App worktree、日常 8790、日常 Heart 或 VPS Ember。

## 已通过

| 场景 | 实际检查 |
| --- | --- |
| 正常安静 | Heart 候选由 App 202 接收；真实模型链路完成；不新增聊天消息；Ember 持久 done |
| 重复接收 | 同一候选重试保留原 run_id |
| 工具白名单 | 实际 MCP 清单与 start.allowed_tools 合并后，模型仅获得 read_memory_source，不提供 search_memory |
| 正常可见 | 只提交一条 assistant；Ember 通过 App 原文路径核对正文后 done；Heart wake_end 为 applied_live |
| 原文 503 | App 保留 pending outbox；Ember 固定完成意图并进入 finishing |
| 双端重启补投 | 503 期间停止并重启 App、Ember；finishing 保留。恢复原文后等待 App 原生重试定时器，原 outbox 正文字节完全相同，不重跑模型，Ember 完成原 done |
| 真实工具绑定 | 模型实际发出 read_memory_source，App 通过 MCP 携带 run/tool_call 身份；Ember GET 中有对应 tool_name、tool_call_id，App 工具账仅一条。此处故意查询不存在的合成事件，验证拒错和调用归属，不声称取到了该事件原文 |
| App 中断恢复 | 模型流挂起时终止 App；重启查询真实 Heart/Ember 账，原执行以 error 关闭，不再次调用模型 |
| 新窗口取消 | 模型挂起时调用真实 context-start HTTP 入口，推进活动/窗口；原 wake cancelled，不发布半截文字，Ember 终态 cancelled |
| 记忆副作用 | 全部场景后隔离 events.jsonl 仍为空，主动 run 未创建普通长期 Event |

本轮没有发现需要修改 App、Heart 或 Ember 生产实现的接线问题。新增文件仅为联调夹具、可复跑脚本和回执。

## 复跑

需要 App worktree 已安装 Node 依赖，Python 已有 Ember requirements 和 Heart 依赖。PowerShell 在 Ember 根目录运行：

```powershell
$env:WAKE_APP_ROOT = 'D:\deliz\.claude\worktrees\wake-integration'
$env:WAKE_HEART_ROOT = 'D:\your-heart'
$env:WAKE_REPORT = Join-Path $env:TEMP 'ember-three-party-report.json'
node --import 'file:///D:/deliz/.claude/worktrees/wake-integration/server/node_modules/tsx/dist/esm/index.mjs' scripts/check_wake_three_party.mjs
```

可用 `WAKE_PYTHON` 显式指定 Python 可执行文件。报告仅含检查名称及隔离边界，不含凭据或聊天正文。`scripts/wake_heart_fixture.py` 是测试专用启动器，不能用作日常 Heart 启动配置。

## 尚未覆盖和下一步

本轮证明跨进程协议、持久恢复和取消链路，不代表真实模型行为、自主 Heart 定时策略、用户日常对话体验或生产网络已经验收。未测试 Heart 自身崩溃恢复。日常 `/ember/runs/*` 仍未部署，App 日常 8790 仍未加载 wake worktree；本回执不改变开关。

后续部署需同时对齐 Ember runs 实现、App 该 worktree、独立 wake 接收凭据和 Heart 投递配置，再安排受控日常验收。第二阶段普通召回引用/完成水位的下一轮真实聊天核对仍是独立事项，不能用本轮合成对话替代。
