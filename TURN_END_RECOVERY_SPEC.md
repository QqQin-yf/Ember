# 普通生成迟到收轮：给 App §6.3 的明确回执

2026-10-21。已读 `D:/deliz/APP_第二阶段回复.md` §6，核对 App 提交 d62aad1 的查询/收轮客户端与 Ember 当前实现。**需要补发原轮次 turn_end。** 本文澄清现有接口，不新增业务接口、不修改 App 代码，不代表真实三方联调或生产验收。

## 1. 何时补发

补发需要同时满足两个条件：

1. Ember 原生成已可靠登记：首次成功 start 已得到并持久保存原 turn，或 `GET /ember/recalls/{recall_id}` 默认 refs 查询返回 200、status=ready，且核对实例、原 session_id、recall_id、真实用户 anchor 和正整数 turn 一致。
2. App 本地已持久提交该生成的真实终态及所需证据。done 必须有属于原生成的已提交非空最终回复和真实 assistant seq；error/cancelled 必须有对应实际终态，不拿停机或超时自动猜终态。

两者哪个后到，就在其持久确认后把收轮任务置为可投递：

- 聊天先结束、召回后查到 ready：用原终态补收轮。
- 召回先恢复、聊天仍在生成：只登记原 turn 并等待真实终态，不提前结束。
- App 重启：从原生成/消息和持久收轮意图恢复，符合两项条件再投递，不重跑模型或工具。
- ready 但 events=[] 或没有合格事件引用：仍需收轮；收轮不依赖是否有内容可转 Heart。

建议在原生成终态与真实回复提交的同一 App 本地事务中登记独立收轮意图，键为 `(instance, recall_id, ordinary_turn_end)`。原 turn 尚未知时保留 waiting_registration；恢复原 turn 后固定请求并独立重试。只靠一次内存回调不足以跨 App 重启补齐。

收轮与 Heart 初次召回登记/resolution 独立，不互相等待或借用 outbox 确认。快照内容的迟到解析可以继续；补收轮不使旧快照的 unresolved 变成已证明来源。

## 2. 身份与请求

沿用 `POST /ember/hooks/turn_end`、原 Bearer 和原 `X-Heart-Instance`。done 示例：

```json
{
  "session_id": "original-session",
  "recall_id": "r-original-assistant-seq",
  "turn": 7,
  "status": "done",
  "assistant_message_id": "102",
  "assistant_reply": "这一次生成实际提交的最终回复"
}
```

turn 必须是 Ember 原响应/查询给出的轮号，不是 App 轮号、Heart turn_id 或当前新 turn。recall_id 沿用原值，不从其中的数字推测消息；assistant_message_id 从绑定原生成的真实消息读取全局 seq，不能换为重生成的兄弟回复、当前 leaf 或预分配但未提交的 seq。不补造用户消息，也不重发 turn_start。

error/cancelled 同样使用原 session_id、recall_id、turn 和实际 status；省略 assistant_reply、assistant_message_id（服务入口会按空正文/null 处理）。部分输出不作为 done，不填假正文或假 seq。已经实际完成的生成不能因为补投超时而改成 error/cancelled。

收轮请求没有新增 event_id 或幂等请求头，普通 MCP 的 Tool-Call-Id 不用于收轮。幂等身份由实例和 recall_id 确定，session/turn 用于归属校验。

## 3. 幂等、回执与重试

同一生成只确定一份终态请求：冻结 session、turn、status、最终正文和真实 assistant seq；后续原样重发。可以固定请求正文，也可以持久保存不可变消息引用/版本并校验内容后重建，不另行无限期复制聊天全文。删会话后按 App 生命周期停止补投，不绕过墓碑重建消息。

- 200 且 ok=true、归属和终态匹配：完成；重复同内容返回原回执，不增加 turn 或重复将回复加入活动语料。
- 超时/断线/5xx：保留原请求，独立退避重试。也可先查 `/ember/generations/{recall_id}`；已知 done/error/cancelled 后仍可用原请求重试，交由 end_hash 校验全部终态内容。仅查询得到同一 status/seq 不足以证明正文也相同。
- 409 id_conflict：原身份已收到不同终态内容，保留冲突并人工/程序对账，不覆盖、不换 ID 逃过校验。context_mismatch、invalid_identity/invalid_status 等同样排查原账，不用当前字段修补。
- 409 generation_not_ready：原登记尚不具备可收轮轮号，回查原登记状态，不伪造 turn。
- 404 unknown_recall：核对实例、原账和服务数据，不靠补发 start 或省略 recall_id 退回旧模式。

工具 JSONL、轮映射 JSONL 与 SQLite 终态回执并非一个事务。回执未保存的故障窗口可能重复追加同值映射行，因此这里承诺同身份/同内容的终态结果幂等，不宣称跨存储绝对只写一行。不重跑已发生的工具动作。

## 4. 过期、未知与不可用

| 原召回/查询状态 | 是否补收轮 |
|---|---|
| refs 200 ready，response_available=true/false | 已有真实终态就补；false 只是召回正文已过期 |
| response 查询或重复 start 为 410 response_expired | 改查 refs，确认 ready 后补；不需要取回 injected_text |
| 202 recall_in_progress / recall_recovery_pending、网络错误、5xx | 没有先前可靠开轮成功证据时，保留等待，不盲发 end；按原 ID 查询 |
| 409 recall_outcome_unknown | 没有先前可靠成功证据时，不自动补；保留原终态意图供后续对账，不把它改成取消，也不重跑原 ID |
| 409 recall_snapshot_unavailable | 若只有旧身份而无成功开轮/原 turn 证据，不自动补；若 App 已持久保存原成功 start 的真实 turn，可按原正常收轮任务重试，不需要快照正文 |
| 404 或身份/归属冲突 | 不自动补、不伪造开轮；记录不可用/冲突并对账 |

App 对召回 pending 的一天查询上限不是 Ember 取消生成的信号；到期可以停止自动查询并保留待核对状态，不凭超时制造 error/cancelled。已确认 ready 的收轮任务有独立的重试状态，不因召回正文 24 小时 TTL 或召回查询任务停止而自动丢弃。

当前底层 end 入口主要检查生成身份/turn，不替 App 判断“是否已有可靠开轮成功证据”。App 必须按上述状态门槛调用，不能因为某条 unknown 占位已有 turn 就当成成功召回。本回执不扩大 unknown 自动收轮能力。

## 5. 补发的实际效果与边界

补发记录原生成终态和 done 的真实回复 seq 映射。原生成仍是本进程当前 running 上下文时，正常收轮会追加其真实回复并关槽；已经 superseded/interrupted 或重启失去上下文时，仅补历史终态/映射，不往当前新生成语料插入旧回复，也不关闭新生成。

Ember 不持久复制整轮聊天正文，原文仍在 App。因此“补收轮”不等于重建重启前全部内存语料，更不等于自动创建长期记忆 Event。它不重新注入召回、不增加 Heart 刺激、不刷新召回时钟、不恢复旧工具写入权限、不训练耳朵。无需新增唤醒自动 writer。

App 可据本文补接持久收轮任务，并在隔离测试中覆盖“终态先到/ready 先到、正文过期、被新生成替代、重启、收轮回执丢失、unknown 不盲发”。真实三方联调与功能开关启用仍待用户安排。

本次复验：现有生成/快照 55 项测试通过；另用临时库组合实测正文到期→重启→refs ready→建立新生成→补旧 done→同内容重试/异内容冲突，确认原引用不改、新生成不受影响。未改业务代码、未新增测试文件、未重跑完整 191 项，未部署或做真实三方联调。

另提示 App §6.1：id_conflict 可以在 Ember 已接受原身份后出现，不能把所有“failed”都解释为“Ember 没有收下”。可以停止自动处理并标冲突，但须保留原身份供对账，不能用新身份重开来规避。
