# 原文候选维护

维护由独立后台 worker 调用第三方生成模型完成。Event 写入后只投递持久化任务；不等待模型，不依赖 Sleep，前端 AI 不负责判断。sidecar 启动 worker，并定期发现漏投的已写入 Event。任务按实例、Event 及文档版本幂等，进度与维护结果存入 `turn_slices.sqlite3`，支持重启续跑。

## 两阶段

1. 从新 Event 的原文区间收集当时已保存的完整切片候选池。只接受能对应实际对话的提交记录；预览排名不作为维护输入。不重新检索，不改变当时的候选池。候选按切片去重，保留每个触发轮。
2. 第一阶段只提供新 Event 的文档内容与旧候选原文，不提供新对话原文、引用字段或触发轮文本。新 Event 区间内的切片不混入旧候选材料。判为相关则保存 Event 到旧切片的联系，不再进入第二阶段。
3. 对第一阶段尚未建立联系的候选，第二阶段只读取触发它的那些新对话轮，逐轮判断。同一候选的不同触发轮可以有不同结果。明确属于同一件事的细节或后续时，可以为已有 Event 添加原文来源；其他相关情况只保存轮级联系。
4. 两阶段均判无关时，只记录疑似误召回。信息不足单独保留，不自动降低相似度、删除切片或修改候选池。

原文来源尚未绑定或对应切片尚未入库时，任务进入 `waiting_source`，不猜测 AI 回复身份。已完成的阶段单独保存，失败重试不重复已保存的模型判断。结果必须引用材料中的原句，并通过候选身份、触发轮和目标 Event 校验后才能写入。

补充来源使用独立的 `added_source` 归属，不修改原有 span。后续切片召回会读取 `source_memberships` 和 `maintenance_relations`；这些信息供后续组织和去重使用，不直接扩大召回范围或绕过筛选。原文与 Event 内容发生变化时，旧判断停止生效；不改写原文或重新向量化。

## 独立配置

将服务商、生成模型及 key 填入 `config/slice-maintenance.local.json`（已忽略）。格式参考 `config/slice-maintenance.example.json`：

```json
{
  "base_url": "https://服务商地址/v1",
  "model": "生成模型名称",
  "api_key": "独立填写",
  "timeout_seconds": 30,
  "batch_size": 16
}
```

当前客户端采用 OpenAI 兼容的 `/chat/completions` 协议。没有默认服务商或模型，不借用前端对话模型或向量模型的配置。配置缺失时进入 `waiting_config`，不会发模型请求。也可通过 `EMBER_SLICE_MAINTENANCE_CONFIG` 指定配置路径。

`MaintenanceQueue(root).statuses()` 可检查任务状态，`relations(instance)` 返回当前仍有效的联系。新 worker 使用原有 Event 写入接口，无需 App 新增维护调用。

本次仅完成本地后台实现与模拟验证；真实第三方判断需配置模型后再验证。旧链路与切片链路的合流、reranker 和最终呈现属于另外的工作，不由本维护 worker 处理。
