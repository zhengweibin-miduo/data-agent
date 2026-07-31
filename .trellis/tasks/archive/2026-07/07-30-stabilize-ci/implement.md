# 实施计划

## 1. 基线与上下文

- [x] 重新获取并核验 PR #71 的 base/head；确认 `master` 仍为 `347228d` 或先处理新基线。
- [x] 将任务工作树安全快进到 PR #71 当前 head，不产生额外 merge commit。
- [x] 读取 backend database/quality guidelines 和待修改测试完整上下文。

## 2. 最小修复

- [x] 复用现有 snapshot/test factory，为 `test_backfill_then_binlog_converges` 建立匹配的 Meta 数据。
- [x] 为 `test_json_sql_null_and_literal_null_remain_distinct_after_cdc` 建立匹配的 Meta 数据。
- [x] 补充精确清理，避免 Meta/outbox 跨用例污染。
- [x] 不修改 `enqueue_value_refresh` 和 CI pytest marker。

## 3. 验证

- [x] `uv run pytest tests/integration/data_sync/test_cdc_pipeline.py::test_backfill_then_binlog_converges -q`
- [x] `uv run pytest tests/integration/data_sync/test_cdc_pipeline.py::test_json_sql_null_and_literal_null_remain_distinct_after_cdc -q`
- [x] `uv run pytest -m "not tei"`
- [x] `uv run ruff check src tests`
- [x] `uv run pyright src tests`
- [x] `uv run python -m data_agent.settings`
- [x] `docker compose -f docs/docker/docker-compose.yml config --quiet`
- [x] `git diff --check`

## 4. 交付门禁

- [x] 确认 diff 只有测试夹具与 Trellis 任务记录。
- [ ] 提交或推送前再次确认 PR #71 远端 head 未被并发更新。
- [ ] 按用户授权边界执行提交/推送；不得 force-push。
