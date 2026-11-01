# 运行与检查

启动方式见 [README.md](README.md)，接口见 [API.md](API.md)。

## 配置

服务启动时加载项目 `.env`。监听地址、端口、鉴权、数据根目录和实例清单由环境变量配置，修改后重启服务。

| 配置文件 | 内容 | 替代文件环境变量 |
|---|---|---|
| `config/models.json` | 提供方、模型用途、端点、超时、重试、代理和生成参数 | `EMBER_MODELS_CONFIG` |
| `config/lifecycle.json` | 召回阈值、约定时区、pin 上限、时间线预算、提取批次和归组退避 | `EMBER_LIFECYCLE_CONFIG` |

模型凭据填写到 `.env` 中各提供方 `key_env` 指定的变量，JSON 配置只保存变量名。各模型用途保留自己的提供方、凭据和向量空间。

模型配置检查：

```sh
python -X utf8 scripts/check_model_config.py
```

该命令显示模型用途、凭据变量名及是否配置，不发送模型请求。更换嵌入模型或端点后，须为对应空间重新生成向量。

## 实例数据目录

`EMBER_DATA_ROOT` 指定根目录；未设置时使用 `EMBER_DATA_DIR`，再回落到项目 `data/`。

- `default`：`<root>/ember`。
- 其他实例：`<root>/instances/<name>/ember`。
- 根目录已有 `events.jsonl` 且没有 `ember/` 时，`default` 直接使用根目录。

检查时选择一个实例的实际目录，不使用多实例父目录。

## 只读健康检查

```sh
python -B scripts/check_memory_health.py --data-dir <instance-data-directory>
python -B scripts/check_memory_health.py --data-dir <instance-data-directory> --format json
```

该命令不加载 `.env`，不构造服务，不请求模型或原文，不修改数据。SQLite 文件及 WAL 复制到临时目录后读取。

### 检查范围

| 数据 | 检查内容 |
|---|---|
| JSONL | UTF-8、JSON 对象、非有限数值及未写完的尾行 |
| Event / RecallRoute | 字段形状、ID、线索类型、空线索和目标关联 |
| 出处与区间 | 冻结来源冲突、孤立出处、消息编号、轮次和区间格式 |
| pin | 关联的 Event 是否存在 |
| generations.sqlite3 | 数据库结构、生成与工具关联、回执和创建结果关联 |
| relationship_timeline.sqlite3 | 时间线比例、压缩或抽点失败、超预算 |
| organization.sqlite3 | 尚未完成的维护错误 |
| source_sync.sqlite3 | 原文增量同步错误 |
| extraction.sqlite3 | 归组失败数量及达到阈值的连续失败种子 |

同一 Event 或线索的追加记录按其存储语义读取。无冻结出处、缺消息编号、pending / unknown 状态、易失回执空正文及 TTL 后空正文分别统计，不直接判为损坏。

### 输出

| status | 含义 | 退出码 |
|---|---|---|
| ok | 检查范围内未发现问题 | 0 |
| issues | 存在错误或需复核的警告 | 1 |
| inconclusive | 检查期间文件发生变化，需在静止副本复查 | 1 |

参数错误退出码为 2。`--max-findings` 默认 200，只限制明细数量。

每条 finding 包含 severity、code、file，可附 line 和 record_id；不输出记忆正文、线索、原文或错误响应正文。

| code | 含义 |
|---|---|
| invalid_jsonl_record | JSONL 记录不合法 |
| incomplete_jsonl_tail | 尾行可能尚未写完 |
| route_event_missing | 线索指向不存在的 Event |
| conflicting_frozen_source | 冻结出处冲突 |
| created_receipt_event_missing | 创建回执指向的 Event 不存在 |
| relationship_timeline_budget_ratio | 压缩与要点占比达到配置阈值，应调整时间线预算 |
| extraction_grouping_repeated_failure | 种子连续归组失败达到配置阈值 |

检查首尾会比较文件名、大小和修改时间。这不是跨 JSONL / SQLite 的事务快照；运行中目录的异常应在一致副本复查。检查不验证 Entity、关系和向量的语义正确性，也不向原文提供方核验权限或父链。

## 后台任务检查

原文同步状态随 briefing refresh 的 source_sync 返回。关系压缩、抽点及维护异常可用健康检查定位。

提取归组失败按种子切片版本和向量空间退避，退避中的切片不作种子或候选。`extraction.retry_initial_seconds`、`retry_max_seconds` 控制重试间隔，`failure_warning_count` 控制连续失败告警阈值。成功、来源变化或材料已处理后清理对应失败记录。

## 测试与工具导出

```sh
python -W error::ResourceWarning -m unittest discover -s tests
python -X utf8 scripts/export_tool_contracts.py
```

工具导出默认写入 `设计讨论/codex回执/工具规范审阅.md`。`--output` 可指定文件，覆盖已有文件使用 `--force`。
