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
模型按用途统一配置在config/models.json，所有key只填.env中对应的key_env变量。入口为ember/model_gateway.py的call_model；模型地址、超时、重试、代理和日志不由各业务模块自行实现。config下旧example文件仅为禁用的用途引用，供隔离验收使用；不再填写模型或key。

## 当前链路

Event/Entity原有入口与原文切片向量入口并行。原文候选经过reranker，再按来源归属组织；认知/规律有独立召回及pin预算。
Event/Entity最多持续在场三轮，临时召回不写入持久聊天记录。后台独立维护关系、归属、资格和待办，Sleep中由App AI正式提取、审阅、评分及pin/unpin。
旧Sleep挂关系/结构维护HTTP入口只返回410，旧观察写入链已删除。历史记忆、关系、来源与retired route仍可读取，主动检索保留相应状态标注。

## 技术文档

- [API.md](API.md)：HTTP/MCP入口、鉴权、错误和数据字段。
- [MEMORY_LIFECYCLE_SPEC.md](MEMORY_LIFECYCLE_SPEC.md)：当前召回、分池维护、待办聚合与Sleep材料契约，含切片实现细节。
- [PROTOCOL_DETAILS.md](PROTOCOL_DETAILS.md)：briefing、召回引用/快照、主动run及来源恢复协议细节。
- [OPERATIONS.md](OPERATIONS.md)：运行说明及只读离线检查。

## 验证与离线工具

```sh
python -W error::ResourceWarning -m unittest discover -s tests
python -B scripts/check_memory_health.py --data-dir <instance-data-directory>
```

Direction离线split/mount以及Cue/Direction向量构建脚本保留；模型分别引用direction_split、direction_mount用途；--engine仅作一次性的本地/远程选择，地址与模型仍取统一配置。
部署状态应查询实际服务/发布快照，源码提交不代表服务已切换。

## 文档发布规则

只跟踪必要的运行、接口和维护技术文档；Markdown使用.gitignore白名单。讨论、理念、对接往来、验收回执和开发计划保留本地，不提交GitHub。备份在忽略的.local-backups目录。

模型配置只读检查：`python -X utf8 scripts/check_model_config.py`，只显示用途、模型、key变量名及是否已配置，不显示密钥，也不调用模型。
切换嵌入模型或地址会改变切片向量空间；旧Cue/Direction索引需要按相同recall_embed用途重新构建，不能混用不同空间。
本地工具规范审阅稿可用 `python -X utf8 scripts/export_tool_contracts.py` 导出，默认文件被忽略；覆盖人工修改必须显式加--force。
