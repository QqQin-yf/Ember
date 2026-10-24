# App §11 验收回执与 Ember 覆盖核对

> 当前状态入口（2026-10-24）：[CURRENT_STATUS.md](CURRENT_STATUS.md)。下文按各段日期保留历史验收记录；旧的“未部署”不覆盖日常 sidecar 已部署 `f910d4d` 的事实，代码部署也不等于第二阶段已开启或三方联调完成。

更新（2026-10-21）：本次新增真实进程强杀四个检查点并全部通过，#5a 已补齐；精确原文迁移与真实 App 临时库路径验收通过。下文对两项“未完成”的描述是历史审计结论，现以 SOURCE_PATH_VALIDATION.md 和 PRODUCTION_PREREQUISITES.md 为准；生产提供方/实例归属实测仍未完成。

生产分类补充：App §12.3 的 Ember 结论见 PRODUCTION_PREREQUISITES.md。#5a 在来源修订生产开启前补齐，#5c 完整三方异常组合可在明确范围和限制下后续补测；下文“无新增接口阻塞”不代表这些生产前置已关闭。

2026-10-21。已读 `D:/deliz/APP_第二阶段回复.md` §11，核对 App a7d82f3、Ember 3e62af6 的测试与已记录真实探针。App 报告历史补交、先召回后补交、首次直接 resolved、独立 resolution 和重复操作幂等均通过三方对账；本次接收该验收报告，未重跑 App/Heart，不将该报告写成 Ember 独立复测结果。

App a7d82f3 对多轮来源的修正与既定契约一致：source_recall_id 是身份标记，不代表该轮必需补交。完整历史轮使用原冻结编号，只有实际缺失的位置使用 completion_bindings；不能要求每个身份标记都有补交。无新增 Ember 接口阻塞项。

## 1. §11.4 逐项核对

| App 未覆盖场景 | Ember 已有证据 | 覆盖边界 |
|---|---|---|
| 补交事务中断后恢复 | test_source_completion.py 的 test_revision_transaction_rolls_back_and_recovers_from_done_receipt：事务写入绑定和候选后主动抛 OSError，断言二者均回滚，再构造新服务从 done 回执恢复；test_lost_terminal_ack_and_historical_done_recover_after_restart 覆盖终态提交后 ACK 丢失 | 是异常注入与服务对象重建，**不是**写事务中途杀 OS 进程 |
| 实际进程重启后修订不重复 | 上轮隔离服务真实 stop/start 后，通过 HTTP 比较原 snapshot、候选与 revision；test_done_retry_query_and_restart_never_increase_revision_twice 覆盖持久幂等 | 真实重启发生在补交已提交之后，不替代上项崩溃窗口验收 |
| 被替代生成的历史 done | test_existing_recall_changes_only_candidates_and_preserves_original_response 中先开 reader 替代 writer，再结束 writer，验证候选变化且新活动槽保持；test_generations.py 的 test_late_old_completion_does_not_close_or_pollute_new_generation | 本地测试与上轮真实 HTTP/MCP 探针均覆盖；App §11.2 第 2 项另有三方部分覆盖 |
| 召回正文过期后补交 | test_expired_response_still_exposes_completed_sources_without_extending_ttl：候选 revision=1，正文仍不可用、response 查询 410、原 expiry 不延长 | 本地时间替身测试；没有等待真实 24 小时或构造三方过期链路 |
| cancelled/error 不补证据 | test_cancelled_or_error_never_supply_assistant_evidence、test_no_source_binding_is_published_before_real_done | 本地反例；不声称三方真实 generation_completed 取消/失败反例通过 |
| 跨会话/错误身份 | test_wrong_terminal_identity_and_content_conflict_cannot_replace_binding、test_legacy_wrong_anchor_and_cross_session_cannot_be_repaired、test_legacy_wrong_anchor_with_matching_session_is_still_rejected、test_explicit_pending_mismatch_is_not_treated_as_legacy | 本地核验反例；仍缺三方真实恶意/错误补交请求链路 |
| 跨实例 | test_other_instance_has_no_bindings；test_generations.py 的 test_same_ids_in_other_instance_do_not_share_receipts 与真实 ASGI test_concurrent_instance_headers_do_not_leak_context | 存储/路由及同 ID 工具隔离已测，不冒充三方来源补交跨实例反例 |
| 内容版本/既有编号不一致 | test_changed_snapshot_source_or_content_version_is_never_overwritten、test_snapshot_with_different_known_messages_is_not_repaired、test_known_original_assistant_is_not_replaced_by_writer_reply；test_recall_snapshots.py 的历史 Event revise/pin 不重建快照用例 | 本地控制版本/编号不匹配、验证不覆盖；未验真实 revise→补交→App/Heart 全链路，不扩大成所有内容变化策略已验收 |
| 原文提供方未配置 | test_source_ledger.py 的 test_unconfigured_raises、test_read_source_unavailable_when_nothing、test_quote_corpus_memory_only_when_source_unconfigured | 只覆盖不可用降级纪律；实际历史原文读取仍不可用，旧 source 客户端迁移未完成 |

测试名称均对应当前仓库 tests/ 下文件。没有把 test_recall_snapshots.py 的 SystemExit 注入称为真实进程退出：该用例同样在同一测试进程捕获异常。

## 2. 集中缺口及归属

1. **Ember：事务中途强杀子进程的恢复验收。** 需要专用临时库，在未提交写事务的明确检查点终止子进程，再用新进程核对绑定/候选/revision 是否一致、从 done 恢复且只推进一次。当前异常回滚测试与正常真实重启都不能代替它。此处是尚未完成的测试，不是已发现的数据损坏。
2. **Ember：混合多轮来源的自动正向回归。** 现有多轮负例验证“缺历史编号时仍 unresolved”；尚无 Ember 自身“前两轮完整冻结且带 source_recall_id、仅第三轮补交”的完整正向构造。App §11.2/11.3 已真实通过该场景，应保留为后续 Ember 自动回归，避免只依赖人工联调。
3. **三方：过期和错误补交的真实端到端矩阵仍未跑。** 本地反例支持当前接口纪律，但不能据此删除 App 报告的未覆盖项。是否再做三方组合验收另按范围安排，不重复要求 App 已通过的正常 resolved/resolution 主线。
4. **既有功能边界继续单列。** 原文提供方、旧 /api/source 迁移、元数据删除通知仍是后续工作；valid echo 的 backfill 与 ODE/耳朵核对由 Heart/App 负责，Ember 不新增协议或代为验收。

## 3. 本轮状态

本次复跑来源完成/快照/生成相关 77 项测试通过，没有新增测试或修改业务代码，没有提交新运行版本或重启服务。隔离服务仍加载 3e62af6；本次只更新验收与覆盖文档，不修改生产开关、凭据或唤醒调度。

此前 213 项完整测试结果与真实服务探针结果保留为原交付证据；本次没有重新执行全部 213 项。以上两项 Ember 专项缺口已集中记录，未发现新的协议阻塞；不把“没有新增阻塞”等同于所有故障场景均已完成验收。
