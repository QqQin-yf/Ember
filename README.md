# Ember

旁路记忆服务，通过HTTP hooks和MCP提供写入、召回、来源下钻、briefing与独立提取。
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

Ember 提供 MCP + hooks。Event 按 daily / relationship / project / promise 分类；模型按原话或切片写入、pin、处理约定和独立提取。输入只算一次查询向量；Event 命中附 Entity summary，切片自身与上级分别过门槛，Direction 不参与自动召回。

Event 落库后及 briefing 刷新时从共享游标拉原文增量，后台筛选、绑定新旧切片，维护主题、Entity 和只追加的关系时间线。提取队列只收无上级、有信息量切片，按字数分批，给结论才完成，可随时停。briefing 截断时保存五档冻结快照，普通轮次不替换。

Sleep 只保留 App 的做梦与沉淀。Sleep 专属材料及旧 link / structure HTTP 入口均返回 410，生产代码不再要求 Sleep 上下文。详见下方生命周期说明。App 原文增量和 hooks 输出仍需由 App 侧适配；本分支提交不代表运行服务已升级。

## 技术文档

- [API.md](API.md)：HTTP/MCP入口、鉴权、错误和数据字段。
- [MEMORY_LIFECYCLE_SPEC.md](MEMORY_LIFECYCLE_SPEC.md)：当前召回、Event/Entity、关系时间线、pin、约定与独立提取契约。
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
