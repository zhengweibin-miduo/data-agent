# 实施计划

## 1. 持久化契约

- [x] 在 SQLAlchemy Core 与 `docs/docker/mysql/data_sync.sql` 同步增加 `progress_column_id`。
- [x] 扩展 claimed work 契约，使 claim 返回当前游标。
- [x] enqueue 新版本时清空游标；相同版本保持游标。
- [x] 增加完整 authority CAS 的 `advance_progress()`，保存游标并释放租约。

## 2. 可恢复刷新

- [x] 固定 eligible 字段为 ID 升序。
- [x] 将 Elasticsearch 写入和最终旧版本清理拆为两个内部 interface。
- [x] dispatcher 每次只处理游标后的一个字段；成功后持久化游标并结束本次工作。
- [x] 无后继字段或空计划时只执行 finalize，成功后沿用现有 acknowledge。
- [x] 保留 lease renewal、LocalProjectionError、远程 backoff 和迟到写 reconciliation 语义。

## 3. 回归测试

- [x] outbox SQL 测试：claim 游标、新版本清游标、advance progress 完整 CAS。
- [x] runtime 测试：两个字段跨三次执行依次写入、推进、finalize；已完成字段不重做。
- [x] bootstrap parity 测试继续通过。

## 4. 验证命令

```bash
uv run pytest tests/unit/metadata_indexing/test_outbox.py
uv run pytest tests/unit/metadata_indexing/test_runtime.py
uv run pytest tests/unit/data_sync/test_tables.py
uv run ruff check src tests
uv run pyright src tests
uv run pytest -m "not integration"
uv run python -m compileall -q src tests
git diff --check
```

若 Docker daemon 可用，再执行真实 MySQL/Elasticsearch 相关 integration；不可用时在交付说明中如实记录。

## 5. 风险点与回滚

- 风险集中在 `repository.py` 的 authority CAS 和 `dispatcher.py` 的 finalize 时序；任何清理不得发生在最后字段游标持久化之前。
- schema 变更只新增 nullable 列。回滚应用代码时可保留该列；停止 dispatcher 并重建派生索引即可恢复旧执行路径。
