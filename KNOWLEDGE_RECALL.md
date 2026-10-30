# 候选池归类、App 提炼与来源下钻

## 独立触发

- 每轮自动召回的切片候选池（原始相似度 >= 0.6）落库后立即入队，独立 worker 使用智谱 GLM-4.7 归类，不等待 App 回复或记忆写入。传给模型的只有冻结候选中的旧切片；当前输入、当前回复、整个新 span 都不参与。预览和主动检索不投递维护任务。
- Event 写入后仍走独立的两阶段归属维护。原文尚未就绪时等待，已完成阶段不重复执行。离散切片 Event 按具体来源收集历史候选，不虚构覆盖区间。
- 两类任务与原文摄取、知识向量化分别在 sidecar 的后台线程中运行。持久化任务、租约、失败重试和漏投恢复不依赖 Sleep。

## 第三方与 App AI 的边界

第三方仅输出 `same_information`、`pattern`、`same_event` 原文分组及逐字证据；分组只是供阅读的材料，不是已经成立的事实或记忆。后台不会创建认知/规律/Event，也不会 pin/unpin。

App 的下一轮召回前缀会提示未处理分组。MCP 工具提供以下流程：

1. `list_memory_groups` 看分组，`read_memory_slices` 按 group_id 或 slice_ids 读取原文。
2. 自主决定是否提炼。认知/规律用 `write_memory_knowledge`，需要正文、触点和 source_slice_ids；Event 用 `write_event_from_slices`，需要完整 Event 格式、recall_routes 和 source_slice_ids。
3. 用 `finish_memory_group` 标为 reviewed 或 dismissed。同样的切片内容再次分到同一种组，不会让已处理任务反复出现。来源内容变化则使用新的分组身份。

新工具仍经过普通 generation 的工具调用身份与幂等回执。原文读取结果保持易失，不把工具原文写入持久化回执。Wake 的工具权限没有改变。

## 认知／规律

独立存储，不伪装成 Event 或观察条目。创建后默认尝试 pin 到 briefing，正文异步用已有 `Qwen/Qwen3-VL-Embedding-8B` 向量化，并保存单独的向量格式命名空间。

固定部分预算为 **1000 tokens**。Ember 没有 App 模型的分词器，当前使用 **UTF-8 字节 token 上界**保守限额，返回 `count_method=utf8_bytes_upper_bound`；这会比实际模型 token 数多，中文可用量偏小。预算计算包含实际固定部分的条目 ID、类别和正文。它不是字符数或精确的 App 模型计数。

超预算时新内容仍保存，但不会强行 pin；回执返回现有条目及 `budget_review_required`，由 App AI 判断重要程度，使用 `set_knowledge_pin` 先取消部分 pin，再 pin 新条目。后台不自动淘汰。briefing refresh/get 始终读取当前 pin 状态，避免缓存保留已 unpin 的文本。

取消 pin 后仍保留触点、内容向量及来源：触点命中的条目额外呈现；纯向量相似度 **>= 0.5** 时额外呈现最高的一条。它们不占旧 Event/Entity 路线的 3 个主名额。已 pin 的条目不重复自动浮现。原观察附加席及结构维护中的观察生成/合并已停用，旧数据保留。

## 混合召回与 VL reranker

旧 Event/Entity/Cue/Direction 路线保持原来的严格判定和主名额，不交给 reranker。切片路线独立使用硅基流动 `Qwen/Qwen3-VL-Reranker-8B`，复用已有 embedding 服务配置中的 key。两类模型分别为 VL Embedding 和 VL Reranker。

请求协议依据[硅基流动官方 rerank 文档](https://docs.siliconflow.cn/docs/api/rerank-post)。

配置参考 `config/slice-reranker.example.json`，实际启用文件是被忽略的 `config/slice-reranker.local.json`，也可设置 `EMBER_SLICE_RERANK_CONFIG`。示例默认禁用，防止未配置测试或新环境发真实请求。本地已启用。用户暂定的 reranker 通过门槛分别为：纯文本 **0.7**（`min_score`）、图片 **0.5**（`image_min_score`）、图文混合 **0.55**（`mixed_min_score`）。当前只处理文本，图片和图文门槛为后续接入保留。原文呈现预算为 **4000 字符**，这些参数均独立可调；reranker 分数与向量相似度不是同一分数空间，这些门槛尚未经过充分标定。

呈现按实际已展示的 Event 归属处理：

- 所属 Event 已展示：命中原文作为该 Event 的补充来源，归入同一单元。
- 所属 Event 没展示：只补充命中切片并标明来源 Event，不自动带出整个 Event。
- 无 Event 归属：按独立切片呈现角色与原文。

Entity 概览不等于成员 Event 已展示；不因归属推导其全文被呈现。原始完整排名和候选池保留，reranker 的排名及实际呈现结果作为额外审计信息保存。reranker 失败只停用本轮切片注入，旧路继续返回结果。已提交 generation 的重放不重复调用模型。

本次使用文本切片。模型选择已兼容后续图片能力，但尚未实现图片抓取、落库或多模态请求组装。

## 精确下钻

`read_memory_slices` 可按 slice_ids、group_id 或 memory_id 读取，三种方式择一。认知、规律、Event 都保存具体来源切片 ID、内容 hash、原会话及人/AI 消息 ID。多会话、多时间的来源保持离散，不能用首尾组成大范围 span。原文变化或缺失时如实报错，不静默换成别的回复。

`search_memory_slices` 用自然语言主动找原文并返回可下钻 ID。`read_memory_source(event_id)` 也能读取新建离散来源 Event。工具层已支持 App AI 使用；本次未修改 App 的原文浏览 UI，也未重启 App 刷新其工具缓存。

验证包括离线回归、实际 HTTP/MCP 的发现/读取/写入/幂等/unpin 链路，以及 GLM-4.7 归类与 VL reranker 的构造样例真实请求。真实输入外发验证最初被自动审批拒绝；用户随后明确授权后，已用两条真实输入及其 79 个候选切片成功调用 VL reranker，结果保存在本地忽略报告 `data/vl-reranker-real-input-check.json`。该报告保留调用时使用的 0.5 门槛；随后用户暂定文本/图片/图文门槛为 0.7/0.5/0.55，未据此改写历史报告。

文本后端链路已经接通，尚未在实际 App 会话中完成“后台分组送达 → App 模型自主阅读和提炼 → 写入/pin → 后续轮再次召回和下钻”的完整实用验收。App 工具缓存未主动刷新，原文浏览 UI 未改动。没有对真实记忆批量运行维护，没有修改 VPS。
