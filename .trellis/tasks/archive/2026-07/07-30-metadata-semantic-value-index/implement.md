# 实施计划

## 1. 契约与 Meta schema

- [x] 在 shared semantic models 增加值索引决策、敏感度和 evidence 三态契约；所有字段补齐中文 `Field(description=...)`。
- [x] 扩展 LLM prompt 与 deterministic validation，覆盖合法 `index`、合法 `skip`、`unknown`、越界 evidence、空 reason 和决策冲突。
- [x] 为 `column_info` 增加 `index_profile JSON NOT NULL`，同步 SQLAlchemy Core、bootstrap SQL、repository upsert 和 schema parity tests。
- [x] 保持 `examples` 原语义，不把索引运行状态写入 Meta。

## 2. 配置与索引投影

- [x] 在现有 Qdrant/Elasticsearch settings 增加独立 metadata collection/index 名称；增加最小 metadata-index settings（top-N、dispatch batch、租约/重试、projection version），同步 YAML 与配置测试。
- [x] 新建根级 `metadata_indexing` 模块及 typed projection models。
- [x] 实现 Qdrant collection setup/strict verification、稳定 UUID5、TEI dense + Qdrant BM25 upsert/delete/search。
- [x] 实现 Elasticsearch strict mapping verification、稳定文档 ID、bulk refresh、旧 refresh version 清理和限定 column IDs 的 value search。
- [x] 在 DDL worker lifecycle 中先 setup 两个索引，再启动任务处理；补 mapping/vector mismatch 启动失败测试。

## 3. Durable desired state

- [x] 在 `data_sync` schema 增加 `metadata_index_outbox` Core/bootstrap 定义，包含 desired version、operation/object identity、租约、available_at、attempts、last_error_type 和 timestamps。
- [x] 实现 desired-state upsert、claim、ack、backoff、dead-letter backlog、rebuild scan；所有时间使用数据库时钟。
- [x] Meta snapshot 事务写 semantic upsert/delete desired states，证明失败时与 Meta 一起回滚。
- [x] DW backfill/event 事务按表合并 value-refresh desired state，证明外部索引失败不影响 DW、游标和事件确认。

## 4. Dispatcher 与刷新

- [x] 实现短事务 claim → 无事务外部工作 → 短事务 settle 的 dispatcher，并接入既有 arq maintenance。
- [x] semantic dispatcher 从当前 Meta 重建 search text 与 projection，调用 TEI/Qdrant 幂等写入。
- [x] value dispatcher 从 Meta 读取 eligible 字段，从 DW 有界聚合高频前 10,000 个非空值，执行 ES refresh-version upsert/cleanup。
- [x] 合并 backfill/replaying 期间的重复表刷新请求；新 desired state 覆盖处理中旧版本时，旧 worker 不得错误确认。
- [x] completeness 只在 `streaming` 且无 pending value refresh 时为 true；DW 空表保留字段资格并允许后续刷新。

## 5. 内部检索与重建

- [x] 实现 metadata semantic search：Qdrant candidates → Meta 批量回读和当前 fingerprint 校验。
- [x] 实现 value search：必须传候选 column IDs，返回 typed values 与 completeness，不返回未经校验的索引 payload。
- [x] 实现仅针对配置目标的全量重建：recreate structure、扫描 Meta、enqueue semantic/value desired states。
- [x] 不新增 HTTP、Conversation 或最终 SQL 生成入口。

## 6. 验证

- [x] `uv sync --locked`
- [x] `uv lock --check`
- [x] `uv run ruff check src tests`
- [x] `uv run pyright src tests`
- [x] `uv run python -m compileall -q src tests`
- [x] `uv run python -m data_agent.settings`
- [x] `uv run pytest -m "not integration"`
- [ ] `uv run pytest tests/integration/persistence tests/integration/data_sync`
- [ ] 可用时运行真实 MySQL/Qdrant/Elasticsearch/TEI 集成：验证空 DW 先建索引、partial 增量刷新、streaming complete、幂等重跑、失败重试和全量重建。
- 未执行原因：Docker Desktop 未运行，`localhost:3306` 不可连接；不得将服务不可用报告为测试通过。
- [x] `docker compose -f docs/docker/docker-compose.yml config`
- [x] `git diff --check`

## 7. 风险与回滚点

- Meta 和 `data_sync` bootstrap schema 没有迁移框架：任何既有环境升级必须在发布前单独审批精确 ALTER，测试不得删除共享 volume。
- DW top-N 聚合可能昂贵：先使用合并 outbox + debounce；只有监控证明不足时才增加专门统计结构。
- 外部刷新不是原子操作：pending desired state 和 refresh version 必须让中途失败保持不可完整使用并可幂等重试。
- LLM structured contract 变化会影响现有快照：在持久化改动前先完成 fake model、repair 和 checkpoint retry 回归。
