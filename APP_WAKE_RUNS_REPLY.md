# Ember 对 App wake-integration 的接入回复

2026-10-24。已读 `D:/deliz/.claude/worktrees/wake-integration/WAKE_INTEGRATION.md` 及实际 wake.ts。Ember 工作区已实现独立 runs/start、runs/finish、runs/:run_id 以及逐请求 MCP run/tool_call 绑定。**未部署日常，未改 App worktree 或 8790，未开启主动发送。** 精确字段及状态见 [RUNS_SPEC.md](RUNS_SPEC.md)。

## 请求/响应确认

App 当前 start/finish 的字段、kind=wake、固定操作 event_id、UUID 叶/截断引用与 seq 的分工可接收；origin=assistant_initiated 由 Ember 响应说明，不要求 App 加普通 user/turn 身份。

- start：200，status=running，返回同一 run/wake/session。start.messages 只收真实消息的编号和角色，不收提醒/正文。
- finish：200，返回同一 run/wake/session 和终态；quiet/null、工具元数据形状与现有适配一致。可见回复需由精确原文路径核对，失败不假报完成。
- query：只有确无记录才 404 run_not_found；保留 running/finishing/真实终态及只读对账字段。

result_ref 接收 `wake-tool:<run_id>:<tool_call_id>`，明确为 App 本地元数据指针，不当作长期事件的专用证据。当前不需要为此临时开放新取工具正文接口。

## App 已对齐的两点（真实隔离联调通过）

1. App 已移除 search_memory，并与 start.allowed_tools 取交集；真实 MCP 清单接入后模型只获得 read_memory_source。写记忆等工具继续禁用，不静默转普通 _live_turn。
2. App 已支持 503 + finishing 按原身份补投。本轮实际让原文接口返回 503，再重启真实 App 和 Ember，确认 finishing 保留，原文恢复后原 outbox 正文字节不变、模型不重跑，最终完成。实际内容/路径冲突 409 仍须明确对账。

重启不重跑工具或模型；完成的只读结果正文不持久化，GET 不返回原文。start/finish 都不会创建长期事件、自动召回、投递 Heart echo 或改变普通 turn。主动写入与工具结果证据继续作为后续能力，不用 pending 永久搁置冒充已支持。

## 验收范围

本地已验证独立 run 的启动、幂等、首终态、quiet/visible、精确路径/正文核验、503 待核验意图、取消/失败、工具重放/未知结果、重启上下文丢失、实例隔离及禁止写入。最终完整回归 293/293、旧 sidecar 38/38 通过；主动 run 专项 24/24（包括真实 HTTP/MCP 和两个实际进程崩溃点）在资源警告按错误模式下通过。

另外复跑 App 既有隔离 wake-http-check：20 项真实 App 进程检查通过。导出的 3 条真实 App run-start 请求，直接交给 Ember 校验器全部通过。它只证明实际 start 字段适配和 App 原隔离测试，不冒充完整真实三方已跑通。

随后完成真实三方隔离 HTTP 联调：20/20 通过，覆盖 quiet/visible、真实 MCP 绑定、503 固定意图补投、App/Ember 重启、流中断恢复及窗口活动取消。详见 [WAKE_THREE_PARTY_VALIDATION.md](WAKE_THREE_PARTY_VALIDATION.md)。模型和触发时机为测试夹具，日常未部署、未开启主动发送。
