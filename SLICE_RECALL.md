# 原文切片召回

自动召回保留Event/Entity既有路线，并增加切片分支；现行整体协议见[MEMORY_LIFECYCLE_SPEC.md](MEMORY_LIFECYCLE_SPEC.md)。

当前整条输入使用config/slice-embedding.local.json中的VL模型现算一次向量，只比较当前实例、同模型/输入格式、当前内容hash的ready向量。候选余弦阈值为>=0.6，按未四舍五入值判定；完整排名与候选池持久化到slice_recall.sqlite3。

候选池出生即交后台归类，不等本轮回复。只有切片候选走VL reranker，文本/图片/混合门槛为0.7/0.5/0.55，现行正文调用为文本。命中所属Event已呈现时作为原文补充，否则独立呈现切片，不强制带出整个Event。

同一会话排除当前锚点及之后的切片；App传入当前分支实际在场消息ID，完全在场的来源不重复召回。不因曾召回而永久禁用。不召回、已取代及合并旧条目统一过滤。

配置/模型失败如实返回unavailable，不编造原文命中。旧Event/Entity路线仍独立保留。生成快照按recall_id幂等，重试不重新向量化或重排。

## 诊断

```sh
python -B scripts/preview_slice_recall.py --query "query" --data-dir <instance-directory>
```

预览保存HTML/JSON，正文转义后呈现。HTTP沿用鉴权及实例路由：

- POST /ember/recall/slices/preview，JSON为input字段；仅诊断，不创建聊天或Event。
- GET /ember/recall/slices/{trace_id}，获取排名。
- GET /ember/recall/slices/{trace_id}?view=sources，按ID和hash读取当前仍匹配的原文。

历史快照保留当时门槛与结果，不事后改写。
