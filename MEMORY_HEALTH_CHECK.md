# 离线记忆库体检

无需 App、Heart、模型或网络。必须显式选择一个实例的实际数据目录；不加载 `.env`，不自动扫描其他实例，不自动修复。

```bash
python -B scripts/check_memory_health.py --data-dir /opt/ember1/data
python -B scripts/check_memory_health.py --data-dir /opt/ember1/data --format json
```

Windows 使用实际实例目录，例如：

```powershell
python -B -X utf8 scripts/check_memory_health.py --data-dir 'D:\your-heart\data\ember' --format json
```

目录遵循实例实际配置：旧扁平库可以直接是 `data/`，新默认实例可以是 `data/ember/`，其他实例通常是 `data/instances/<name>/ember/`。不要把上例当作本机当前配置，也不要把整个多实例父目录当作一个库。

## 检查范围

- 当前目录所有 `.jsonl` 的 UTF-8、JSON 对象格式和非有限数值；不输出行正文。
- Event/RecallRoute 的模型形状、非空 ID、线索状态与类型、空 cue、线索指向不存在的事件。
- turn_spans/pinned 的事件关联、v1/v2 区间合法性。
- 第一条冻结出处的冲突、孤立出处、区间、消息编号格式和消息所在轮次。冻结出处先于事件提交，孤立记录仅作警告。
- 如有 `generations.sqlite3`：在临时 DB/WAL 副本执行 quick_check，核对工具/快照的生成身份关联、回执 JSON、持久 completed 回执缺失和 created 回执指向丢失事件。

同一 Event/Route 多条追加是正常历史，不因重复 ID 报损坏；旧记忆无冻结出处、未知/缺失消息编号、合法 pending、unknown/in_progress 回执、volatile 空结果及 TTL 后空正文单独统计或保留原义，不冒充有效证据。

## 输出与退出码

`status=ok` 表示覆盖范围内未发现问题；不是完整业务正确性证明。`issues` 表示存在错误或需要人工复核的警告。`inconclusive` 表示检查期间数据文件发生变化，关联告警可能是写入中的暂态，应在静止副本复查。退出码 0 对应 ok，1 对应 issues/inconclusive；参数错误使用 argparse 的退出码 2。

每条 finding 仅含 severity、code、文件名及可选行号/记录 ID；不输出摘要、core、cue、摘录、聊天正文或工具结果正文。`--max-findings` 默认 200，仅限制明细条数，不影响问题计数或 inconclusive 状态。

常见 code：`invalid_jsonl_record`（非法整行）、`incomplete_jsonl_tail`（可能未写完的末行）、`route_event_missing`（线索断链）、`conflicting_frozen_source`（冻结来源冲突）、`invalid_turn_spans`（非法区间）、`created_receipt_event_missing`（成功回执所指事件缺失）。

## 只读和一致性边界

不会实例化 Store/Service、恢复生成、清理过期快照、写入记忆或访问外部服务。读取原始 SQLite 仅使用普通文件读取；DB/WAL 复制到系统临时目录后才打开 SQLite，SHM/临时文件只可能在副本目录创建，结束后清理。不会使用忽略 WAL 的 immutable 方式读取实时库。如发现非空 rollback journal，则报告需要复核并跳过数据库检查，不自动恢复，也不把未恢复状态直接判为损坏。

检测首尾的文件名/大小/mtime 变化；这不是跨 JSONL/SQLite 的事务快照，不能保证识别全部并发变化。对正在运行的库，结果只能作为只读诊断；确认异常时应复查一致的离线副本。未检查 Entity/Relation/向量的语义关联或新增主动 runs.sqlite3 的生命周期语义，也未向 App 验证原文与权限；不根据缺来源补造数据。

2026-10-24 首次只读检查 VPS `/opt/ember1/data`，8 事件、36 线索、4 冻结来源，未发现所覆盖的结构/关联错误。4 条旧事件无冻结来源、1 个来源轮次缺完整编号，保留原状态；无生成 SQLite。该结果是一次快照，不是持续健康保证。

## 验收记录

2026-10-24：新增专项 15/15 通过，覆盖合法追加修订、坏 JSON/截断末行、无效来源编号、pending/null、冻结来源冲突、断链、SQLite WAL、未恢复回滚日志、原库字节与 mtime 不变、变化时 inconclusive、明细截断及 CLI。VPS 隔离目录完整 267/267 unittest 与 136 项独立验收通过；最终版本只读复查现有库的结果与首次一致。
