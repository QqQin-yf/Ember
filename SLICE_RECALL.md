# 原文切片自动召回：先看完整排名

2026-10-30：用户选择先看匹配排名，再定候选门槛、最终呈现门槛和条数。本阶段不设置阈值、不截断匹配列表、不向聊天注入结果。相似度高低仅为诊断结果，不代表最终判定有关。

自动轮前入口改为：`当前整条用户输入现算一次向量 → 同模型的原文切片向量 → 余弦相似度排序 → 保存完整排名`。使用 `config/slice-embedding.local.json` 中指定的 `Qwen/Qwen3-VL-Embedding-8B`；没有可比切片时如实返回 no_vectors，不调用旧模型兜底。有切片但配置/模型失败时返回明确 unavailable 状态，也不回退旧自动路线。

不再从自动入口调用 Cue 词面/语义、Entity 名称/别名、Direction、观察条目附加席、旧多源名额规则或旧关系替换。主动 `search_memory`、手动 Event 检索和既有工具/出处功能保留。旧 Event 渲染流水线仅作为明确调用的 legacy 函数保留，测试通过 `tests/legacy_event_fixture.py` 显式使用它，验证历史响应快照、出处完成绑定及渲染兼容；默认 EmberService 的自动钩子不会调用它。

只比较当前实例、当前模型及输入格式、当前切片内容版本的已完成向量；维度不匹配的向量不比较。对于同一会话，排除当前输入锚点及之后的切片、整个内容已在当前窗口内的切片。不会因为命中原文就带出其所属 Event/Entity，也不自动建立永久关系。无信息量判定与最终语义判定尚未加入。

完整排名和本次输入向量保存在实例目录的 `slice_recall.sqlite3`。排名记录切片身份、内容指纹、消息编号、分数及直接命中入口；不另存切片正文或 key，不改 Event 本体。正文预览通过已有切片编号按需读回，并核验内容指纹。此处是未设门槛的诊断排名，尚不是“达到候选门槛”的正式候选池。

普通轮前钩子保留 App 既有字段，增加 `slice_recall`（trace_id、状态、数量等诊断信息）；`injected=false`、`injected_text=null`，`recall_refs.events=[]`。新生成的诊断信息随原有生成快照冻结，重试相同 recall_id 不重新向量化或重排。不恢复之前已冻结的旧响应为新结果。

本地查看：

```powershell
python -B scripts/preview_slice_recall.py --query "想测试的一条输入" --data-dir data
```

默认生成 `data/slice-recall-preview.html` 和同名 JSON，保留全部排名；HTML 可逐项展开原文，正文经过 HTML 转义，不把历史内容当脚本执行。

HTTP（沿用 sidecar 鉴权、实例路由）：

- `POST /ember/recall/slices/preview`，JSON：`{"input":"想测试的一条输入"}`。仅做独立诊断，不创建假聊天、普通生成或 Event。
- `GET /ember/recall/slices/{trace_id}`：完整排名。
- `GET /ember/recall/slices/{trace_id}?view=sources`：附带指纹仍匹配的切片原文。

本次仅本地与 GitHub 交付，VPS 和日常 App 连接保持用户指定的现状。后续查看结果再确定门槛、注入数量和 Event/Entity 的呈现判定。
