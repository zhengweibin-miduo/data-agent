# Meta 字段语义与取值索引设计

## 1. 目标与边界

Meta MySQL 是表、字段、指标及字段索引决策的真相源。Qdrant 和 Elasticsearch 是可删除、可重建的检索投影：

- Qdrant `metadata_semantic`：表、字段、指标的 Dense + BM25 混合语义检索。
- Elasticsearch `metadata_column_values`：高价值、非敏感字段的业务值检索。
- `data_sync` 只保存刷新 desired state、租约、重试和完成版本，不保存业务值。

本任务只提供内部模块，不新增 HTTP、公共 DTO、Conversation 或最终 SQL 生成入口。

## 2. 模块与依赖方向

新增根级业务模块 `src/data_agent/metadata_indexing/`，由它拥有索引 setup、投影、刷新、检索和重建：

```text
models/semantic.py
  -> LLM 字段索引决策

ddl_metadata snapshot
  -> Meta column_info.index_profile
  -> metadata index desired state

data_sync DW transaction
  -> 合并表级 value-refresh desired state

metadata_indexing dispatcher
  -> MySQL claim
  -> TEI / Qdrant / Elasticsearch（无 MySQL 行锁）
  -> MySQL compare-and-set settle

future internal caller
  -> metadata_indexing search
  -> Qdrant semantic candidates
  -> Elasticsearch values filtered by column_id
```

`metadata_indexing` 可依赖共享 models、settings、infrastructure 和 persistence；`data_sync` 与 `ddl_metadata` 只依赖它的 typed desired-state 写入接口，彼此不反向依赖。

## 3. LLM 字段索引决策

### 3.1 Structured output

在 `SemanticColumn` 增加一个嵌套的 `ColumnValueIndexProfile`：

```text
decision: index | skip | unknown
sensitivity: non_sensitive | sensitive | unknown
reason: bounded string
evidence: bounded list[existing table_id | column_id]
```

现有 `SemanticColumn.confidence` 继续服务原有字段语义分类；字段值索引资格不使用这个数值，也不新增数值置信度。

### 3.2 决策依据与门禁

LLM 每次接收整张物理表上下文：表名、角色、描述、字段名、类型、注释、结构关系。真实 DW 值永不进入 LLM。

字段获得值索引资格必须同时满足：

1. `decision == index`；
2. `sensitivity == non_sensitive`；
3. evidence 非空且只引用当前 schema 的真实 ID；
4. reason 非空且 bounded；
5. 决策与敏感度不冲突。

`unknown`、证据缺失、证据越界或 `index + sensitive` 进入现有一次 repair；仍不合法则整个 semantic result 按现有契约拒绝，不写 Meta。

### 3.3 Meta 持久化

`column_info` 新增 `index_profile JSON NOT NULL`，保存上述结构化决策。它是字段语义事实的一部分；索引刷新版本、租约、重试、partial/complete 状态不进入 Meta。

`examples` 保持现有展示样例语义，不复用为策略或索引状态。

仓库无迁移框架：SQLAlchemy Core 与 `docs/docker/mysql/meta.sql` 必须同步更新。已有环境需要部署前执行经审批的 `docs/docker/mysql/upgrades/20260730_metadata_semantic_value_index.sql`（同时增加 `table_info.alias` 与 `column_info.index_profile`，并为既有字段回填保守的 `skip + unknown` 决策），或在可丢弃环境重新初始化；运行时不得自动改表。

## 4. 索引结构先建

DDL worker 启动时，在处理任何任务前幂等执行：

1. 创建或严格校验 Qdrant `metadata_semantic` collection；
2. 创建或严格校验 Elasticsearch `metadata_column_values` index；
3. mapping/config 不兼容时启动失败，禁止让服务自动创建错误 mapping。

### 4.1 Qdrant 语义文档

一张表、一个字段或一个指标对应一个稳定 point：

```text
point_id = UUID5("data-agent-metadata:{kind}:{object_id}")
search_text = canonical name + aliases + description + table context + metric definition
payload = kind, object_id, table_id, role, data_type, schema_fingerprint, projection_version
```

collection 使用当前 TEI 1024 维 dense vector，并增加 Qdrant server-side BM25 sparse vector；查询使用 Qdrant RRF fusion。投影只返回 object ID，内部服务再从 Meta 读取并校验当前对象。

### 4.2 Elasticsearch 字段值文档

一个字段值对应一个稳定文档：

```text
document_id = SHA256(column_id + canonical_value)
column_id, table_id
value_text, value_keyword
frequency
refresh_version
schema_fingerprint
```

mapping 使用 `dynamic: strict`、既有中文 analyzer、keyword 精确字段和数值频次。查询必须限定候选 `column_id`，组合精确 term 与 text match，不允许无字段范围的全局值命中直接进入问数。

## 5. 异步 desired state 与一致性

新增 schema-qualified `data_sync.metadata_index_outbox`，使用一条合并型 desired state 表而非逐行事件表：

- semantic 对象按 `(target, object_kind, object_id)` 合并；
- 字段值刷新按 `(target=values, table_id)` 合并；
- desired version 更新会覆盖未完成旧请求；
- claim 使用数据库时钟、短事务、租约和 `FOR UPDATE SKIP LOCKED`；
- 外部调用期间不持有 MySQL 行锁；
- settle 必须匹配 desired version 与 lease token；
- 远程失败有界退避并保留 dead-letter 行；
- stale worker 不得确认新 desired state。

Meta snapshot 在同一 MySQL 事务中写 Meta 与 semantic desired states；事务提交后由 dispatcher 写 Qdrant。Meta 成功不等待 Qdrant。

每个 DW backfill/event 批次在原事务中仅 upsert 当前表的 value-refresh desired version；Elasticsearch 失败不回滚 DW。

## 6. 字段值刷新

获得表级刷新租约后，dispatcher：

1. 从 Meta 读取当前表仍有效且具有值索引资格的字段；
2. 从当前 DW 表按字段聚合 `COUNT(*)`，每字段取高频前 10,000 个非空值；
3. 为本次刷新生成单调 `refresh_version`；
4. bulk upsert 当前值文档；
5. 删除该字段旧 refresh version 的文档；
6. 全部成功后 compare-and-set 确认 outbox。

DW 空表产生成功的空刷新，不改变字段资格。新数据到达后新的 desired version 再次刷新。表在 `backfilling/replaying` 时允许生成 partial 值投影；只有 `data_sync` 当前为 `streaming` 且该表无 pending value desired state 时，内部检索才标记 `complete=true`。

逐批刷新请求在 outbox 中合并并设置短 debounce，避免回填每批都并发执行全表聚合。频次和淘汰由当前 DW 快照重算，不实现难以校正的逐行计数器。

## 7. 内部检索

内部接口保持两个深模块：

```text
search_metadata(query, kinds, limit) -> typed Meta object candidates
search_values(query, column_ids, limit) -> typed value candidates + completeness
```

第一步用 Qdrant 混合检索候选表/字段/指标并回读 Meta 校验；第二步只在候选 `column_id` 范围内查 Elasticsearch。索引 payload 不作为权威业务返回。

## 8. 重建、故障与回滚

- 手动重建必须要求调用者提供与配置完全一致的 collection/index 名称；只删除项目自己的两个目标。
- 重建先 recreate/verify structure，再从 Meta 扫描 semantic objects，并按 eligible table enqueue value refresh。
- 单一目标故障只保留 pending desired state；Meta、DW 和另一目标不回滚。
- mapping/vector dimension 不匹配是确定性 dead-letter，并在启动或重建阶段显式失败。
- 回滚代码时可停止 dispatcher；Meta 新增的 `index_profile` 是向后兼容的附加事实，已有业务读取不依赖它。删除派生索引不会丢失权威数据。

## 9. 取舍

- 采用表级合并刷新而非逐行 ES 计数：代码更少，能正确处理删除、频次变化和 top-N 淘汰；代价是刷新时需要对 eligible 字段做聚合查询。
- 采用独立 outbox 而非在 DW/Meta 事务中调用外部服务：保留权威事务的短时性和可恢复性；代价是最终一致性。
- 不为 LLM 决策引入人工白名单、字段名硬规则或数值置信度；证据三态让缺失上下文显式进入 `unknown`。
