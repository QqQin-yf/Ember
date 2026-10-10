# Ember v3 MVP

Ember 是一套事件中心的记忆系统：以 Entity 为长期锚点、Event 为基本记忆单元、Cue 为事件自己的召回入口、Event Relation 为事件间的结构联系。本仓库是 MVP 实现，与 heart（梦通道的接收方）通过一次性批量推送对接。

完整设计见仓库外的 `ember-v3-架构文档.md`（momo 私有文档，不入库）；对接契约见 heart 仓库的 `记忆系统对接契约.md`。

> 历史阶段的 README（第一阶段/第二阶段验收记录）见 git 历史（`git log -- README.md`）。

## 依赖

**零第三方依赖**——纯 Python 标准库（urllib / json / http.server / dataclasses）。Python ≥ 3.10 即可（用到 `str | None` 联合类型语法）。

## 快速开始

```bash
git clone https://github.com/QqQin-yf/ember1.git
cd ember1
cp .env.example .env    # 填入真实 key（智谱 LLM / 硅基流动 embedding / heart 地址）

# 起服务（对话代理，含记忆读写）
python proxy.py         # 默认 127.0.0.1:8765

# 离线任务（都是手动触发）
python scripts/sleep_run.py --engine glm   # Sleep 维护（裁决/归属/收紧/summary）
python scripts/mount_directions.py         # Direction 挂载（Entity 达 8 事件后）
python scripts/build_cue_vectors.py        # Cue 语义向量预计算（新 cue 后/换模型后）
python scripts/build_direction_vectors.py  # Direction 向量预计算（换 embedding 模型后）
```

## 目录

```text
proxy.py            HTTP 入口：/v1/chat/completions（OpenAI 兼容）+ 观察端点
ember/              核心库（每个文件开头有职责说明）
  service.py        对话编排：召回 → 前缀注入 → 嵌入式写入 → 存档
  recall.py         免费召回：Cue 词面/语义 + Entity/Direction 通路
  store.py          Event/Cue 持久化（jsonl append-only）
  sleep_v2 等脚本    裁决/进度账本/收紧/Relation/Entity 生命周期
  turn_ledger.py    与 heart 的 turn_id 映射账本（旁路）
  dream_source.py   梦选材：主料(未入梦) + 联想渗漏 + 无来由的梦
  dream_push.py     heart 梦通道 HTTP 客户端（一次性批量推送）
  dream_ledger.py   夜账本：已入梦标记 + 每夜留痕
data_real/          真实记忆数据（events/entities/routes/relations/账本/向量）
tests/              21 套测试 + e2e，逐文件可独立运行
scripts/            离线任务入口 + 取数实验脚本
```

## 测试

```bash
# 每套独立运行（无需任何外部服务；LLM 用 Mock）
python tests/test_dream_channel.py   # 24 项
# 全量：逐个跑 tests/test_*.py，另加 tests/e2e_fake_pipeline_v2.py
```

## 红线

- 认知对象（Entity/Event/Cue/Relation）与运行时/派生状态严格分离——账本一律旁路文件
- Event 不存 activation/权重/分数；强度由多 Cue 共命中聚合涌现
- 面向 Claude（TA）的文本不出现"身体"，一律 "heart"
