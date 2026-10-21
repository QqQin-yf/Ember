# Ember v3 MVP

Ember 是一套事件中心的记忆系统：以 Entity 为长期锚点、Event 为基本记忆单元、Cue 为事件自己的召回入口、Event Relation 为事件间的结构联系。

形态：**旁路 sidecar**（hooks + MCP + 简报/钉住 + sleep 夜间维护接口），消费方是 app（Deliz）。对接契约见本仓库 `API.md`。

> 历史阶段的 README（早期验收记录 / 代理形态 / heart 梦通道）见 git 历史（`git log -- README.md`）。

## 依赖

sidecar 依赖 MCP 官方 Python SDK（`mcp>=1.8,<2`，自带 starlette/uvicorn）；其余模块纯标准库。Python ≥ 3.10。

## 快速开始

```bash
git clone https://github.com/QqQin-yf/ember1.git
cd ember1
cp .env.example .env    # 填入真实 key（智谱 LLM / 硅基流动 embedding）；跨机部署必设 EMBER_TOKEN

# 起服务（旁路 sidecar：钩子 + 简报/钉住 + sleep 编排 + MCP 工具）
python sidecar.py       # 默认 127.0.0.1:8767（EMBER_HOST / EMBER_SIDECAR_PORT 可改）

# 离线任务（都是手动触发）
python scripts/mount_directions.py         # Direction 挂载（Entity 达 8 事件后）
python scripts/build_cue_vectors.py        # Cue 语义向量预计算（新 cue 后/换模型后）
python scripts/build_direction_vectors.py  # Direction 向量预计算（换 embedding 模型后）
```

Sleep 夜间维护（挂关系 → 结构维护 → 沉淀证据）由 app 编排，接口契约见 `API.md` §6。多实例（一颗心一套记忆库，`EMBER_DATA_ROOT` / `EMBER_INSTANCES`）见 `API.md` §1 / §10。

第二阶段已实现普通生成身份绑定、工具回执和稳定召回快照；临时召回正文保留 24 小时，到期保留引用和防复用身份，不删除记忆或聊天。接口见 [RECALL_SNAPSHOT_SPEC.md](RECALL_SNAPSHOT_SPEC.md)，本机隔离交付见 [PHASE2_INTEGRATION.md](PHASE2_INTEGRATION.md)；未部署生产或完成真实三方联调，主动 run 仍待实现。

## 目录

```text
sidecar.py          HTTP 入口：钩子组 + 简报/钉住 + sleep 编排 + MCP（streamable-http）
API.md              全量对外契约
ember/              核心库（每个文件开头有职责说明）
  instances.py      多实例注册表（一颗心一套记忆库，X-Heart-Instance 路由）
  service.py        编排：召回 → 前缀注入 → 嵌入式写入 → sleep 维护
  recall.py         免费召回：Cue 词面/语义 + Entity/Direction 通路
  store.py          Event/Cue 持久化（jsonl append-only）
  sediment.py       沉淀证据账本（版本挂证据，app 转交追加）
  pair_seen.py      判过的 pair 留痕（补跑不重复裁决）
  turn_ledger.py    turn_id ↔ message_id 映射账本（旁路）
  source_refs.py    新事件固定来源账（只存明确消息身份，缺证据不猜）
  source_completion.py 同生成完成绑定与独立来源候选修订（不改原快照）
  generations.py   普通生成身份、工具回执与 24 小时召回响应快照（SQLite 旁路）
  journal.py       可恢复记账的 JSONL 持久追加
data/               记忆数据（events/entities/routes/relations/账本/向量，多实例时按实例分子目录）
tests/              unittest 套件，discover 全量运行
scripts/            离线任务入口 + 取数实验脚本
```

## 测试

```bash
py -m unittest discover -s tests   # 全量（无需任何外部服务；LLM 用 Mock）
```

## 红线

- 认知对象（Entity/Event/Cue/Relation）与运行时/派生状态严格分离——账本一律旁路文件
- Event 不存 activation/权重/分数；强度由多 Cue 共命中聚合涌现
- 面向 Claude（TA）的文本不出现"身体"，一律 "heart"
