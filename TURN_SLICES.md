# 逐轮原文切片（2026-10-30）

本步提供逐轮原文切片存储，sidecar 轮后自动链路见 [SLICE_PIPELINE.md](SLICE_PIPELINE.md)。以下手动命令仍可用于独立补跑。普通轮为用户消息一条、助手回复一条；用户侧为系统消息时，只保存助手正文。也可以只传助手编号，保存没有用户消息的助手轮。

```powershell
python -B scripts/save_turn_slice.py --data-dir data --session-id <会话编号> --user-message-id <用户消息seq> --assistant-message-id <助手消息seq>
```

系统生成的用户侧消息增加 `--user-is-system`；没有用户消息时省略 `--user-message-id`。多实例传 `--instance` 和对应实例的 `--data-dir`。

原文仍由 Deliz 的 `/api/source/path` 提供，沿用 `EMBER_SOURCE_URL`、`EMBER_SOURCE_TOKEN`、`EMBER_SOURCE_TIMEOUT`。工具保存的是原文接口提供的文字，不是模型重新整理后的文字；助手正文遵循提供方已有的可见正文规则。传入的两个编号应是已知的一轮用户消息和对应回复；当前接口仅核验同一路径，不负责从任意两条消息判断是否属于同一轮。

切片存于指定数据目录的 `turn_slices.sqlite3`，含实例、会话、双方消息编号、保留的消息及内容指纹。系统侧编号可保留作来源身份，正文在存储前排除；不靠文本关键词猜测系统消息。存储层识别 `role=system` 或明确的 `is_system=true`；当前 Deliz 接口只提供 user/assistant，未暴露系统标记，使用者需明确传入 `--user-is-system`。

同一对来源编号重复保存不会增加切片；重新拉取发现内容改变时更新同一份检索副本；不同实例、会话、重新生成的回复编号分别存储。失败不写不完整切片。CLI 会重新打开数据库核验保存结果，只输出状态、编号和长度，不打印正文或凭据。

sidecar 轮后自动链路已接入；历史全量回填、编辑/删除自动同步和召回尚未接入。这里不创建 Event、不判断经历边界，后续完整索引协议和数据模型继续按讨论定稿。

## 切片向量化

在 `config/slice-embedding.local.json` 的 `api_key` 中填入硅基流动 key。这个文件由 `config/.gitignore` 排除，不进入 GitHub；可提交的空白模板是 `config/slice-embedding.example.json`。切片使用自己的配置，不影响原有 Event/Cue/Direction 的模型或向量。

```powershell
python -B scripts/embed_turn_slices.py --data-dir data
```

默认模型为 `Qwen/Qwen3-VL-Embedding-8B`，接口为 `https://api.siliconflow.cn/v1/embeddings`，按[硅基流动官方接口文档](https://api-docs.siliconflow.cn/docs/api/embeddings-post)发送单个文本字符串。一轮完整切片（含 user/assistant 角色分隔）生成一个向量，不把人和 AI 分开生成，不摘要、不截断；系统侧已在保存切片时排除。使用模型默认维度，记录实际返回的维度。

向量保存到同一个 `turn_slices.sqlite3` 的 `slice_embeddings` 表，记录来源内容指纹、提供方、模型、输入格式、维度、向量和状态。原文保存成功不依赖向量成功。已有且内容未变的向量跳过；失败或源切片改变的，下次运行补算；模型或提供方改变时分开保存，不混用。请求期间原文改变时，不落入过期向量。

手动批处理每次最多处理 100 片，可用 `--limit` 修改；失败记录固定错误码，不保存服务端错误正文或凭据。每片单独请求，下次运行重试失败项。轮后自动任务由 sidecar 后台补算，不主动扫入其他历史切片。没有加无信息量判定或召回门槛；这里的向量就绪不等于允许召回。当前仅本地和 GitHub 开发，不部署 VPS。
