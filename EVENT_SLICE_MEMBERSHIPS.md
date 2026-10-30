# Event span 与切片的归属

归属是派生的多对多索引，存在实例 `turn_slices.sqlite3` 的 `event_span_sources` 与 `event_slice_memberships` 表。切片、原文、已有向量保持原样；不按 Event 复制切片，不重新向量化。一个切片可被多个 Event 的 span 覆盖。

连接依据是：实例、会话、Event/来源版本一致、可信冻结来源中的 Ember turn 落在 span 内，以及精确的消息编号。匹配助手编号，并在来源提供用户编号时同时核对用户编号；不把消息 seq 的数值区间当作 turn span，不从用户编号推测重新生成的哪条回复。来源只有用户编号而助手尚未完成时等待可靠完成绑定；既有 `source_completions` 证据可以补齐，冻结来源不被改写。仅有助手消息的切片及排除了系统侧正文的切片仍可关联。

没有冻结身份、来源未验证、版本/会话/span 冲突或消息身份歧义的 Event 标记为 unresolved，不猜测归属。来源已知但切片尚未存储时保留来源定义，切片之后入库即可自动关联。Event 来源版本变化时重建对应归属，重复执行幂等。

自动接线覆盖两种顺序：

- 写 Event 和 span 后同步归属；同轮完成的来源绑定成功后再同步。
- 保存新切片时读本地已有 Event 来源并同步，同时连接已登记的来源定义。

显式补跑：

```powershell
python -B scripts/link_event_slices.py --data-dir data --event-snapshot data/event-span-snapshot.json --output data/event-slice-memberships.json
```

不传 `--event-snapshot` 时读取同一实例目录已有的 `events.jsonl`、`event_sources.jsonl`、`turn_spans.jsonl` 和完成绑定。快照只包含 Event 元数据及来源，不含 key。归属可以在没有任何向量的切片上先建立，之后的向量通过 slice_id 自然共用。

原文检索排名与候选池现在会保留 `span_memberships`（Event ID、版本、turn、span、来源指纹）。这只是区间归属，既不说明每句话都语义属于 Event，也不增加相似度，不自动带出 Event 或复制 Event 正文。当前只建立 span 归属，未建立轮级语义联系或改变旧/新召回分支的合流规则。

本次仅本地与 GitHub 交付。线上元数据只读取得，本地保存快照和派生关系；VPS 不改代码、配置或数据。
