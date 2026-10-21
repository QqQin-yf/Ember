# 精确路径读取与强杀恢复验收

2026-10-21。本次变更替代旧区间读取；不启用生产开关，不部署 VPS，不新增唤醒/写记忆/耳朵协议。App 对照版本 a7d82f3（其未提交的 APP_第二阶段回复.md 仍是交接依据）。

## 精确读取实现

- `ember/source_client.py` 只请求 `/api/source/path`，携带 Bearer 与 `X-Heart-Instance`；严格检查响应完整编号、顺序、角色、文本类型。非 200/超时/坏响应不可用，无 range fallback。
- `ember/source_paths.py` 从本实例持久的原始开轮/终态取得逐轮身份。远程返回必须逐条与该身份吻合，不用旧 max-reply 账本映射身份。
- `read_source_event/read_memory_source`、自动召回内联、briefing/sleep 原文块从冻结 Event 出处和可信来源完成绑定读取。后来的 turn_span、reply max、当前会话内存、当前 leaf 均不能覆盖来源。内容版本不符/缺一轮编号则不读。
- quote_corpus/写入定位和逐字核对、observe/sleep 增量均切换精确读取。观察产物也冻结实际取材编号，不能落成只有范围的“可信来源”。

边界明确：不猜邻近 ±3 轮，`context_expanded=false`；旧 Event 无可信逐轮编号不能翻原文。会话的持久生成身份若混合兄弟分支/重复编号归属不明，整份历史语料拒绝，不能从当前内存重新混入；写入仍可使用本轮实际用户文本，观察不推进游标。当前实现不提供混合历史的自动路径选择/增量补洞，要恢复该能力需另给明确的路径输入，不能恢复旧 range。召回门槛、选材算法未修改。

## 本地及真实 App 验收

完整 `py -X utf8 -B -m unittest discover -s tests`：224 项通过（新增 10 项精确读取测试及 1 项带四个进程检查点的强杀测试）。另运行 11 个独立 main() 回归脚本，全部通过；旧 transport 夹具已同步迁移，无数据集依赖的额外脚本不计为本次执行。

真实测试命令：

```powershell
py -X utf8 -B scripts/check_source_path_app.py --app-dir D:/deliz/server
```

该探针只读 App 仓库，启动其**实际 index.ts 后端**和临时 SQLite；进程用随机凭据、空 .env、独立目录、临时 loopback 端口，Ember/Heart 外连地址及开关关闭，无模型调用；结束后停止子进程。不是重写 readPath 的假服务，也不是生产验收。探针不启动/停止现有 8878、8879。

15 项通过：旧分支、重新生成兄弟、编辑重发、多段不连续且不补中间；拒绝混合兄弟、混合编辑路径、其他会话、未知会话、cancelled、sleep 消息；Ember 事件/内联/briefing 旧路径；混合事件 not_archived；quote_corpus 精确身份；混合 observe 不推进游标；错误 Bearer 拒绝。

**发现并保留的生产缺口：** App `/api/source/path` 目前忽略 `X-Heart-Instance`，未知实例头仍能读取已知会话。测试报告明确 `cross_instance_provider_enforced=false`、`production_config_verified=false`。不能将 15 项路径检查通过称为实例隔离或生产提供方通过。需 App 校验实例与会话归属，或部署上提供可验的独立实例端点/凭据隔离。

## 5a 真实进程终止恢复

命令：`py -X utf8 -B -m unittest tests.test_source_crash`。

`tests/source_crash_worker.py` 在专用临时数据上构造真实 writer done、create 工具已提交回执及两个 reader 快照。父进程等待以下确定检查点后调用 Popen.kill（Windows TerminateProcess），不经过子进程 Python rollback/finally：

| 检查点 | 强杀后的数据库（服务恢复前） |
|---|---|
| binding_uncommitted | done 保留；binding 与两个 view 均未发布 |
| first_view_uncommitted | 已执行第一个 view 的 INSERT；强杀后 binding 与所有 view 均未发布 |
| views_uncommitted | 已执行全部 view 更新；强杀后 binding 与所有 view 均未发布 |
| committed_before_ack | done/binding/两个 view 已发布；终态 ACK 未交付 |

四处均通过：SQLite integrity_check=ok；新进程从持久 done 恢复，两个 reader 都完整 revision=1；重复 done、重复查询及再启动第二个恢复进程仍为 1。原 metadata/response、events.jsonl、event_sources.jsonl、calls 工具回执逐字/逐行不变；无活动写槽/上下文重开。验证范围是进程死亡恢复，不冒充断电/磁盘损坏验收。测试故障注入只存在于子进程夹具，没有给业务服务新增故障开关。

## 交付顺序

Ember 迁移和上述路径验收已完成；**同意 App 开始将旧 `/api/source` 歧义请求改为 HTTP 409**。所有环境的执行顺序仍是“Ember 新客户端已加载→App 收紧旧接口”；本地隔离回执不代表 VPS 已升级，不能让仍运行旧版的生产调用方先承受接口收紧。App 修改后再补验旧请求 409 与 Ember 无回退，本文件不冒称该 App 后续代码已完成。

#2 的 Ember 实现/精确路径验收与 #5a 可关闭；#2 的 App 收紧收尾待 App 回执；#3 仍须实配实测，详见 PRODUCTION_PREREQUISITES.md。#5c 三方真实异常组合维持有明确限制的后续补测。生产开关仍未批准。
