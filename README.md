# Ember

Ember 是供 AI 对话使用的记忆服务，通过 MCP 工具写入和管理记忆，通过 hooks 提供召回和提醒，通过 briefing 在上下文截断后恢复记忆。

对话、模型工具循环和实际上下文由接入方管理。Ember 保存记忆、来源关联和派生索引，并从接入方的原文服务读取对话。

## 功能

| 能力 | 内容 |
|---|---|
| Event | 按日常、关系、项目、约定分类，使用原话区间或切片作为来源 |
| 召回 | 按线索和语义匹配 Event、原文切片、主题概括及认知/规律 |
| Entity | 整理具体对象的摘要，关联 Event，支持摘要订正和成员下钻 |
| 关系时间线 | 按时间组织关系事件，逐件压缩、追加要点，并保留原文入口 |
| 约定 | 管理到期时间、状态和追加日志，跟踪待处理曝光 |
| Pin | 分类管理常驻记忆，支持清单、取消和同义约定合并 |
| 提取 | 分批提供原文组和相似知识，写入使用的切片自动结账，只有不写才需明确跳过 |
| Briefing | 汇总关系时间线、常驻记忆、近期事件、认知/规律和到期约定 |

## 配置与启动

需要 Python 3.10 或更新版本。

安装依赖：

```sh
python -m pip install -r requirements.txt
```

将 `.env.example` 复制为 `.env`，配置服务、原文提供方和模型凭据。

| 配置 | 用途 |
|---|---|
| `EMBER_HOST` / `EMBER_SIDECAR_PORT` | 监听地址和端口，默认 `127.0.0.1:8767` |
| `EMBER_TOKEN` | 入站 Bearer 令牌；监听非回环地址时必须设置 |
| `EMBER_DATA_ROOT` | 实例数据根目录，默认使用项目下的 `data/` |
| `EMBER_INSTANCES` | 实例清单，以逗号分隔，默认 `default` |
| `EMBER_SOURCE_URL` / `EMBER_SOURCE_TOKEN` | 对话原文服务的地址和令牌 |
| `EMBER_SOURCE_TIMEOUT` | 原文请求超时秒数，默认 `8` |

模型、提供方、端点和调用策略配置在 [config/models.json](config/models.json)。按其中的 `key_env` 将凭据填写到 `.env`，使用 `EMBER_MODELS_CONFIG` 可指定替代配置文件。

召回阈值、pin 上限、时间线预算、提取批次和约定时区配置在 [config/lifecycle.json](config/lifecycle.json)，使用 `EMBER_LIFECYCLE_CONFIG` 可指定替代文件。

检查模型配置并启动：

```sh
python -X utf8 scripts/check_model_config.py
python sidecar.py
```

配置检查显示用途、模型和凭据是否存在，不发送模型请求。

## 接入

接入方提供两个原文接口：

- `GET /api/source/path`：按会话和精确消息 ID 返回原文，核验消息所属路径。
- `GET /api/source/increment`：按不透明游标返回已完成的对话增量。

Ember 在 Event 写入后后台拉取增量，在 briefing 刷新时同步兜底。两个触发点共用游标，切片、筛选、嵌入和绑定成功后才推进。

接入方调用以下服务入口：

| 入口 | 用途 |
|---|---|
| `POST /ember/hooks/turn_start` | 在模型调用前获取召回内容和提醒 |
| `POST /mcp/` | 发现并调用记忆工具 |
| `POST /ember/hooks/turn_end` | 提交生成终态、最终回复及消息 ID |
| `POST /ember/briefing/refresh` | 在截断、新会话或重进时生成新的 briefing |
| `GET /ember/briefing` | 读取已保存的 briefing 快照 |

普通生成使用稳定的 `recall_id`；MCP 请求携带 `X-Ember-Recall-Id` 和 `X-Ember-Tool-Call-Id`。接入方将 hooks 返回的召回文本和提醒注入本轮请求，并在需要新 briefing 时显式刷新。刷新会等待原文同步及关系时间线准备，应设置足够的请求超时。

配置了 `EMBER_TOKEN` 时，所有入口都要求 `Authorization: Bearer <token>`。多实例通过 `X-Heart-Instance` 选择，默认 `default`；清单外实例返回 404。原文提供方也须核验实例归属。

## 数据

默认实例保存在 `<EMBER_DATA_ROOT>/ember`，其他实例保存在 `<EMBER_DATA_ROOT>/instances/<name>/ember`。Event 和线索使用 JSONL，原文切片和提取队列使用 SQLite，各实例独立存储。

原文读取保留精确来源身份。部分来源用于写入时，只结掉实际使用的切片；未处理材料保留在提取队列。原文工具结果使用易失回执，进程重启后可重新读取来源。

嵌入索引按模型和向量空间区分。更换嵌入模型或端点后，需要为新空间重新生成匹配的向量。

## 检查与工具

运行测试：

```sh
python -W error::ResourceWarning -m unittest discover -s tests
```

只读检查一个实例的记忆库：

```sh
python -B scripts/check_memory_health.py --data-dir <instance-data-directory>
```

导出工具 schema、字段和实际回执：

```sh
python -X utf8 scripts/export_tool_contracts.py
```

默认导出到本地 `设计讨论/codex回执/工具规范审阅.md`，可用 `--output` 指定路径。覆盖已有文件需要 `--force`。

## 文档

- [API.md](API.md)：HTTP、MCP、参数、鉴权和错误行为。
- [MEMORY_LIFECYCLE_SPEC.md](MEMORY_LIFECYCLE_SPEC.md)：记忆写入、召回、后台维护与提取流程。
- [PROTOCOL_DETAILS.md](PROTOCOL_DETAILS.md)：生成绑定、召回快照、来源引用和主动 run 协议。
- [OPERATIONS.md](OPERATIONS.md)：运行维护与离线检查。
