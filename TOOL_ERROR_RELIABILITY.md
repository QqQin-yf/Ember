# 工具报错与独立验收收尾

> 当前状态入口（2026-10-24）：[CURRENT_STATUS.md](CURRENT_STATUS.md)。下文按各段日期保留历史验收记录；旧的“未部署”不覆盖日常 sidecar 已部署 `f910d4d` 的事实，代码部署也不等于第二阶段已开启或三方联调完成。

2026-10-23。本次完成优先安排的两项：修复旧验收失败，统一写记忆的参数/出处报错与 MCP 失败标记。该次隔离验收未替换 VPS 的线上 checkout 或重启服务；后续部署单独执行。

## 测试失败根因

`tests/test_sidecar.py` 先由模块导入加载本机 `.env`，之后 S12b 临时覆盖原文配置，结束时恢复了本机真实原文地址。S13 本来假设原文服务未配置，却向真实服务请求虚构的 `sc1`，得到 `source_session_unknown`，与预期 `source_unconfigured` 不一致。

验收入口现在显式设置空原文 URL/令牌，并禁止实际网络读取；需要原文的样本使用精确路径替身。S13 严格验证未配置，新增 S14 经精确路径读取可用原文后严格验证 `quote_not_found`，两种拒绝均不创建事件。另有测试用不可访问的外部地址模拟宿主配置，确认不会影响验收结果或访问真实服务。

## 报错行为

- MCP schema 失败：保留字段路径、实际误放路径（如有）、说明和 `hint`。
- 写记忆业务参数失败：增加 `code=invalid_tool_arguments`、精确 `field` 和 `hint`；字符串形式的校验函数接口继续兼容代理调用。
- 原文失败：保留原有 `source_*` 分类，增加相应处理建议。只有 `quote_not_found` 指向 `quote_spans`。
- MCP 外层：明确失败/拒绝标为 `isError=true`；pending、in_progress、not_archived 不因状态名而被标错。
- 同一稳定工具调用的业务失败仍作为持久回执重放，字段、建议和结果不变；未自动修正参数、创建事件或重跑副作用。

## 验证

Windows 本地：相关 unittest 100/100；sidecar 38/38；摘录验收 36/36。

由于 Windows 事件循环创建回环 socket 曾阻塞，完整 HTTP 与进程测试使用 VPS Python 3.10 的隔离临时源码目录。上传包仅含源码、测试与执行器配置，未带 `.env`、聊天或记忆数据；显式清空原文连接配置，数据位于临时目录，测试所需 HTTP 提供方均为本地替身。运行结束已清理临时源码与数据目录，线上服务未切换。

- `python -B -m unittest discover -s tests`：252/252 通过，包含真实 MCP/HTTP 的失败回执重放。
- `python -B tests/test_sidecar.py`：38/38。
- `python -B tests/test_r4_transcript_quotes.py`：36/36。
- `python -B tests/test_phase1.py`：20/20。
- `python -B tests/test_phase2.py`：42/42。

此次验收不代表完成 App/Heart 真实三方联调，也未开启第二阶段或主动 run 功能。
