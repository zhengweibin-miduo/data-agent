# 仓库研究证据

## DDL 与 Meta

- `src/data_agent/models/physical.py:10-20,38-46`：物理字段只包含 DDL 事实，不包含值索引决策。
- `src/data_agent/models/semantic.py:43-65`：`SemanticColumn` 是现有 LLM structured output，可扩展字段值索引三态决策。
- `src/data_agent/ddl_metadata/workflow/llm_metadata_generator.py:34-93`：现有模型调用已使用 Pydantic structured output，并限制为当前物理对象。
- `src/data_agent/ddl_metadata/validation.py:82-101,124-141`：现有确定性校验会验证 evidence 非空且只能引用已知表/字段 ID。
- `src/data_agent/ddl_metadata/persistence/tables.py:21-32`：`column_info` 已保存字段语义，但没有值索引策略。
- `src/data_agent/ddl_metadata/persistence/metadata_repository.py:100-125`：`examples` 当前固定写入空数组，不能承载索引策略或运行状态。
- `src/data_agent/ddl_metadata/persistence/snapshots.py:47-61,111-133`：Meta、DW desired state、memory/outbox 在同一调用方事务中提交；外部索引调用不能进入该事务。

## Qdrant 与 Elasticsearch

- `src/data_agent/infrastructure/qdrant.py:10-49`、`src/data_agent/infrastructure/elasticsearch.py:10-53`：共享异步客户端生命周期可直接复用。
- `src/data_agent/memory/indexing/qdrant.py:27-29,73-99,109-165`：已有稳定 UUID5、collection setup、payload index、幂等 upsert/delete/search 模式。
- `src/data_agent/memory/indexing/elasticsearch.py:25-162,173-232`：已有 strict mapping、中文 analyzer 校验、稳定文档 ID、幂等 upsert/delete/BM25 search 模式。
- `src/data_agent/ddl_metadata/worker/lifecycle.py:87-113`：现有 DDL worker 启动阶段已经初始化 Qdrant、Elasticsearch 和索引 setup，可挂接 metadata index setup。

## DW 与异步投影

- `src/data_agent/data_sync/models.py:25-36`：现有阶段为 `backfilling / replaying / streaming` 等。
- `src/data_agent/data_sync/backfill.py:66-91,162-214`：DW 批次、key ownership、游标或事件确认在同一事务提交，适合只追加一个合并型索引 desired state，不能同步调用外部索引。
- `src/data_agent/data_sync/service.py:129-234`：仅在历史回填完成且缓冲事件清空后进入 `streaming`。
- `src/data_agent/data_sync/repository.py:265-289`：现有只读 readiness 边界可判断字段值结果是否完整。
- `src/data_agent/memory/mysql/index_outbox.py:36-79,103-161`：已有短事务 claim、租约、远程写、compare-and-set settle 和有界重试模式，可复用协议而不复用 memory 表。

## 结论

- 新能力应由根级 `metadata_indexing` 模块拥有，避免 `data_sync -> ddl_metadata` 或复用长期记忆业务模块。
- Meta 保存字段的 LLM 索引决策；Qdrant/Elasticsearch 只保存可重建投影。
- DW 每批只合并一个表级刷新请求；独立 worker 按当前 DW 快照重算高频前 10,000 个值，避免逐行维护频次和淘汰逻辑。
- `streaming` 只决定“完整可用”，不阻止索引结构创建或回填期间的部分值刷新。
