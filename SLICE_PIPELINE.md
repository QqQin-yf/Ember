# 自动原文切片链路

本地 sidecar 收到成功完成的轮后钩子后，执行：

`原文消息编号入队 → 后台拉原文 → 按轮切片并落库 → 向量化 → 向量落库`

普通轮沿用 `/ember/hooks/turn_start` 和 `/ember/hooks/turn_end`；不再需要手动运行两条保存/向量化命令。新生成从已保存的 request_context 取得原始用户编号，旧模式从本轮原始消息身份取得，不猜编号区间或使用 max-reply 映射。只有成功且 status=done 的轮次入队，错误、取消和被拒绝的完成请求不入队。

轮后请求可附 `user_is_system: true`（严格布尔值），此时用户侧正文在保存切片前排除，只向量化 AI 回复。提供方明确标记 `is_system=true` 时也会排除。普通 user 角色没有系统标记时按真实用户消息处理，不根据正文猜测。主动 run 的 `/ember/runs/finish` 成功且 `spoke=true` 时，只取本次 AI 消息，不借用前面的用户消息；静默 run 不建切片。

来源连接继续使用现有 `.env` 中 `EMBER_SOURCE_URL`、`EMBER_SOURCE_TOKEN`、`EMBER_SOURCE_TIMEOUT`；原文提供方必须能读到已提交的最终消息。模型读取 `config/slice-embedding.local.json`，可用 `EMBER_SLICE_EMBED_CONFIG` 指向另一份本地配置。模型仍为 `Qwen/Qwen3-VL-Embedding-8B`。密钥文件及数据不进入 GitHub。

轮后响应保留原有完成结果，附 `slice_ingestion: {status, job_id}`。任务先写入实例目录的 `slice_ingestion.sqlite3`，网络请求在后台工作器执行，不阻塞轮后完成。可以通过 `GET /ember/slices/jobs/{job_id}` 查看状态、切片编号、尝试次数和固定错误码；沿用 sidecar 鉴权及实例隔离。

原文和向量仍保存在 `turn_slices.sqlite3`。原文保存成功与向量完成分开；向量调用失败保留切片，不必重新拉取，后台退避补算。来源失败也保留任务等待重试。重启后继续持久任务；异常中断的工作租约到期后可重领。重复完成请求不会重复生成同一份切片和向量。服务关闭时停止领取任务并等待当前请求结束，网络超时继续由配置约束。

工作器只处理轮后登记的新任务，不扫描和向量化剩余历史切片；之前“只算前四分之一”的历史批次范围不变。尚未接入历史漏推扫描、编辑/删除同步或召回。本次只交付本地代码和 GitHub，不启动或修改 VPS 服务；日常 App 若仍连接 VPS，就不会使用本地这条新链路。

更新：轮前的原文匹配已接入，见 [SLICE_RECALL.md](SLICE_RECALL.md)。候选门槛为相似度 ≥ 0.6，完整排名与候选池均保存；最终呈现规则未定，暂不注入聊天。
