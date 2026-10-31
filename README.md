# Ember

旁路记忆服务，通过HTTP hooks和MCP提供写入、召回、来源下钻、briefing与Sleep正式产出。
对话和模型工具循环由App持有，Ember不代理聊天。运行要求Python 3.10+，依赖见requirements.txt。

## 启动

```sh
pip install -r requirements.txt
# 将 .env.example 复制为 .env，填写端口、鉴权、来源服务及实例目录。
python sidecar.py
```

默认监听127.0.0.1:8767。跨机访问必须设置EMBER_TOKEN。实例通过X-Heart-Instance选择，数据路径由EMBER_DATA_ROOT和EMBER_INSTANCES控制。
切片嵌入、reranker和第三方维护分别使用config下的example模板，填写对应*.local.json；密钥与运行数据不得提交。

## 当前链路

Event/Entity原有入口与原文切片向量入口并行。原文候选经过reranker，再按来源归属组织；认知/规律有独立召回及pin预算。
Event/Entity最多持续在场三轮，临时召回不写入持久聊天记录。后台独立维护关系、归属、资格和待办，Sleep中由App AI正式提取、审阅、评分及pin/unpin。
旧Sleep挂关系/结构维护HTTP入口只返回410，旧观察写入链已删除。历史记忆、关系、来源与retired route仍可读取，主动检索保留相应状态标注。

## 技术文档

- [API.md](API.md)：HTTP/MCP入口、鉴权、错误与数据字段。
- [MEMORY_LIFECYCLE_SPEC.md](MEMORY_LIFECYCLE_SPEC.md)：召回、分池维护、待办聚合与Sleep材料契约。
- [TURN_SLICES.md](TURN_SLICES.md)、[SLICE_PIPELINE.md](SLICE_PIPELINE.md)：精确原文切片和异步入库。
- [SLICE_MAINTENANCE.md](SLICE_MAINTENANCE.md)、[EVENT_SLICE_MEMBERSHIPS.md](EVENT_SLICE_MEMBERSHIPS.md)：关系维护与多对多来源。
- [RECALL_SNAPSHOT_SPEC.md](RECALL_SNAPSHOT_SPEC.md)、[SOURCE_COMPLETION_SPEC.md](SOURCE_COMPLETION_SPEC.md)：幂等快照及出处补交。
- [RUNS_SPEC.md](RUNS_SPEC.md)：独立主动run。
- [MEMORY_HEALTH_CHECK.md](MEMORY_HEALTH_CHECK.md)：只读离线记忆库检查。

## 验证与离线工具

```sh
python -W error::ResourceWarning -m unittest discover -s tests
python -B scripts/check_memory_health.py --data-dir <instance-data-directory>
```

Direction离线split/mount以及Cue/Direction向量构建脚本保留；其engine配置仅有split、mount，独立于在线维护模型。
部署状态应查询实际服务/发布快照，源码提交不代表服务已切换。

## 文档发布规则

只跟踪必要的运行、接口和维护技术文档；Markdown使用.gitignore白名单。讨论、理念、对接往来、验收回执和开发计划保留本地，不提交GitHub。备份在忽略的.local-backups目录。
