# 运行与离线检查

启动及本地配置见[README.md](README.md)。下面是无需网络和模型的记忆库检查方法。


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

## 模型配置与密钥

- 唯一模型配置：config/models.json。providers集中base_url/key_env；purposes按embed、rerank、maintenance、recall_embed、direction_split、direction_mount、direction_local命名。
- key只保存在.env；不同既有凭据通过provider继承显式区分，避免覆盖不同账户。
- 超时、重试、退避、代理、temperature/max_tokens在统一配置设置，入口统一执行。max_tokens未设置时沿用提供方默认；proxy支持environment、direct和env:变量名。
- 参数改动后重启对应服务；不需要在各调用点修改。EMBER_MODELS_CONFIG可选择另一份完整配置，不能把明文key写入其中。
- App Sleep的正式产出模型仍由App控制台管理。Ember maintenance仅是后台第三方判断，不替代App模型。
- 本次将原本地JSON凭据迁入.env。部署时需同步所需环境变量；源码推送不包含凭据，也不启动维护任务。
- `python -X utf8 scripts/check_model_config.py`用于不联网的配置检查。


## 2026-11-01 改造进度

1. Event 字段和写入工具：对应《Event字段调整》第 1–4 节、《写入工具与归组范围》第 1 节。移除旧内容字段，整数 significance，增加 kind / focus 和 promise 基础字段；成对工具共用来源与调用幂等校验，支持区间或离散切片来源；读取与 briefing 以 core 兜底。后续步骤完成前，原有分类维护和 briefing 组装尚未切换。

2. 原文切片：对应《原文切片调整》第 1–7 节、《整体流程》2–3 节及共享游标约定。新增 provider 增量游标与幂等重放、规则/典型废话过滤、区间新旧绑定、按 kind 与上级 core 的独立相似度门槛；有上级只呈现 Event。停用逐轮摄取和逐轮 GLM 池归类。主题概括替换入口将在第 4 步接入，提取归组队列在第 8 步接入。配置均在 config/lifecycle.json；原文接口适配契约见 API.md。

3. promise：对应《Event字段调整》第 3 节、《Briefing与提取工具》第 3 节及《整体流程》本地午夜约定。加入持久曝光账、处理工具与追加 log、改期、到期 briefing、低分完结退出召回、10 分无 due 默认 pin，以及每日午夜/启动补算过时效。低分完结主题概括在第 4 步接入共用归组。

4. 归组与 Entity：对应《Entity》第 1–4 节、《Event分类维护》第 2–3 节、《Event字段调整》第 3 节主题概括。加入共用归组、不可变日常副本及重聚合、完结约定主题层、Entity summary 更新/防重/合并和旧 ID 映射、项目取代；自动召回改为 Event 三席与独立去重卡片，不再走 Direction。关系时间线由第 5 步继续接入。

5. 关系时间线：对应《Event分类维护》第 4 节。实现写入后的独立压缩缓存、总量上下限选择、按批追加要点、event_id 和断开时间区间溯源、要点下钻、pin 历史恢复原样、briefing 等待及模型失败准确降级。健康检查通过副本报告 5:5 比例和失败/超量；关系边仅延续或回应，不取代。

6. Pin：对应《Pin》第 1–4 节。实现四类限额与清单、focus 居中原文、完整下钻、约定 Event 的最早主条合并、跨会话原文/log 保留和旧 ID 映射，以及随 hook 提交确认的单次超预算提醒。原知识 token 总预算改为分类条数与单条字数保护。

7. Briefing 重组：对应《Briefing与提取工具》第 1 节及《整体流程》第 3 节。实现五档顺序、剩余预算砍低分近期条目、关系与 pin 去重、去除自动 10 分及最高召回旧条、末尾待处理约定数；精确现场 ID 支持多会话离散来源，GET 读取冻结快照。约定处理材料按曝光 generation 的确切消息拉取，并保持原文响应易失。

修正 1：对应《Event字段调整》1 与《Pin》2。focus 输入改为逐字原话，复用 quote_spans 的匹配规则，在来源区间内解析为内部消息 ID；聊天和切片写入均在落库前校验，失败返回字段与重摘提示。

修正 2：对应《原文切片调整》5.1。删除逐 trace 入队及 GLM 归类的 PoolQueue / PoolMaintenanceWorker 和生命周期残留；知识嵌入 worker 归回 knowledge 模块。保留召回诊断 trace，但不把它作为归组任务，第 8 步仅从无上级、有信息量的切片供料。

修正 3：对应《写入工具与归组范围》1.1。冷静期使用绑定 generation 的上下文与 briefing 截断点，原文被截走后持久锁定，所有 kind 相同；约定状态/log 处理不受修订限制。两个写入入口均支持可选 supersedes，预先校验目标，保留旧内容和被取代标注，关系事件不允许取代。

修正 4：对应《Event分类维护》4 的 pin 与“要点只往后加”。移除要点作废/整批回滚逻辑；pin 已抽点历史仅额外呈现原样，原要点文字、来源与区间不变。抽点排除仍在现场的来源并封存冷静期；非要点订正仅重算单件压缩。
