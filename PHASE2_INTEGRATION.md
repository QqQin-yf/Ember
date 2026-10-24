# Ember 第二阶段隔离联调交付

> 当前状态入口（2026-10-24）：[CURRENT_STATUS.md](CURRENT_STATUS.md)。下文按各段日期保留历史验收记录；旧的“未部署”不覆盖日常 sidecar 已部署 `f910d4d` 的事实，代码部署也不等于第二阶段已开启或三方联调完成。

最新隔离配置（2026-10-21）：启动器支持 `--source-env-file <仓库外受限文件>`，仅接受 EMBER_SOURCE_URL/TOKEN/TIMEOUT，且原文源限 loopback；不继承日常配置。可配 `--source-sample <合成样本>` 和 `--source-report <仓库外结果>`，在实际监听 PID 启动时执行一次自检，不新增公开调试接口。外部 `scripts/check_source_provider_mcp.py` 再走标准 MCP 读回。8792 提供方的进程联验通过，实例 default/phase2/second；当前 PID/提交看 D:/phase2-secrets/ember-readiness.json，完整结果见 SOURCE_PROVIDER_PROCESS_VALIDATION.md。下文“source 不配置”的描述为早期状态，未显式传此文件时仍默认不配置。生产未验证、未开启。

本轮更新（2026-10-21）：精确路径客户端和 5a 四个进程强杀检查点已验收，见 SOURCE_PATH_VALIDATION.md。真实 App 临时库 15 项通过，生产提供方/实例归属实测仍未完成。8878 继续使用仓库外原凭据和独立数据；具体加载版本以 D:/phase2-secrets/ember-readiness.json 为准，不能将旧段落的版本当成最新。新客户端 source 未配置时历史取材会如实不可用，原文路径不会退回旧接口。生产开关未批准。

2026-10-21。已读 App APP_第二阶段回复.md §7—8，核对最新提交 b929d44。范围为真实 Ember 的呈现/出处、普通生成与工具身份、快照/恢复/24 小时清理及原轮次补收轮。App 的完成活动水位属于 Heart 接口，Ember 不接管该字段。

## 1. 测试实例

本机地址 `http://127.0.0.1:8878`，MCP 为 `/mcp/`，实例清单为 `default,phase2`，逐请求携带 `X-Heart-Instance`。该服务是实际 sidecar，不是 stub；使用标准库 MockLLMClient 作为旁路原有占位，不替 App 生成对话。

启动脚本为 `scripts/run_phase2_integration.py --runtime-dir <私有目录> --port 8878 --instances default,phase2`。运行目录在仓库之外，每次联调分配独立目录，不能指向生产记忆或 Heart 数据。脚本不读项目 .env，并清除子进程继承的 Ember 配置及常用模型 key；原文提供方默认不配置，不会连生产 App。需要跨重启恢复测试时重用同一私有目录。

本机探针与 App 应使用不同的 session_id、recall_id 和 tool_call_id。初始为空记忆库，可通过普通开轮和显式 MCP 写入建立合成测试记忆。旧原文 `/api/source` 迁移未在本段实现；测试服务未配置原文提供方时，依赖远程历史原文的读取/核验会如实报告不可用，不应解释为新路径已接通。

## 2. Token 的本机交付

运行目录位于当前用户 LOCALAPPDATA 的 `Codex/ember-phase2/<本次运行>`，目录 ACL 仅授予当前 Windows 用户及 SYSTEM。`token.txt` 保存随机 Bearer token；`app.env` 保存供 App 隔离子进程读取的 EMBER_BASE_URL/EMBER_TOKEN/EMBER_INSTANCES。不要把这些文件或内容复制进仓库、聊天或日志。

App 可由同一 Windows 用户在本机读取该文件并将变量传入自己的隔离进程。仅凭 App 地址不能访问服务；所有钩子、查询、MCP 均要求 Bearer。token 不通过进程命令行传递。`runtime.json` 仅含非秘密运行信息：提交号、PID、地址、实例、数据/日志/凭据文件路径；具体路径由本次交付消息提供。

这不是修改 App 生产 .env 的许可。App 的隔离数据库、Heart 测试地址、EMBER_RECALL_REFS/HEART_PHASE2 开关由 App 按已授权的隔离联调方案配置。Ember 本次不启动/修改 Heart 或 App，不修改 VPS、防火墙、生产 token 或服务。

## 3. 应对账的接口与行为

- `/ember/recalls/{id}` 默认 refs，不含正文；`?view=response` 取 24 小时内原响应。202 不是成功空召回；unknown/unavailable 不重跑旧 ID。
- `/ember/generations/{id}` 和 `/tools/{tool_call_id}` 核对生成终态与工具回执。
- `/ember/hooks/turn_end` 用原身份补投；done 需真实最终回复和 seq，error/cancelled 不带部分回复。正文过期但 refs ready 仍可补；重复同内容返回原回执，异内容冲突，旧收轮不关闭新生成。
- `/mcp/` 每次 tools/call 携带实例及 X-Ember-Recall-Id / X-Ember-Tool-Call-Id；旧生成禁止新调用，已完成的旧调用可返回原回执。

接口细节分别见 RECALL_REFS_SPEC.md、GENERATION_BINDING_SPEC.md、RECALL_SNAPSHOT_SPEC.md、TURN_END_RECOVERY_SPEC.md。真实三方联调用例按 App §8.4 执行；本机 Ember 探针通过不代表三方验收完成。

主动 run、工具结果专用证据、元数据删除通知和旧 source 客户端迁移仍不在本次交付内；唤醒调度保持关闭，不新增每次唤醒自动写记忆，不训练耳朵。

## 4. 运行管理

在开始前检查端口占用，已有服务不覆盖。服务以隐藏子进程启动，stdout/stderr 重定向到私有运行目录，PID 写入 runtime.json。需要停止或重启时，先核对 PID 的可执行文件、命令行确为本脚本及此运行目录，再只操作该进程；重启沿用原 token、数据目录和启动参数，不删除数据。运行期间的提交号从实际启动时的 git HEAD 写入 runtime.json；后续改代码须明确重启，不能把新工作树当成已加载版本。
