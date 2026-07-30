# 稳定 CI 并修复反复失败问题

## Goal

修复 PR #71 在 GitHub Actions 中连续失败的两条 CDC 集成测试，使 CI 保留真实集成覆盖并恢复通过，而不是通过跳过测试或放宽生产数据一致性守卫隐藏问题。

## Background

- PR #71 基于 `master` 提交 `347228dfa640d681a0bfbdfe053e5ab6396129d8`，最近至少 6 次 `quality` job 都在 `Run pytest suite` 失败。
- 最新已核验失败 run 为 `30522214534`、job `90804967934`；结果为 `2 failed, 328 passed, 1 deselected`。
- 两条失败测试均位于 `tests/integration/data_sync/test_cdc_pipeline.py`，错误为 `RuntimeError: DW 字段未对应当前 Meta 表`。
- 生产路径由 `MetadataSnapshotService.persist` 在同一事务中先持久化 Meta，再写入 `data_sync_task`；失败测试直接构造并持久化 `DesiredSyncTable`，没有创建对应的 Meta 表和字段。

## Requirements

- R1：测试准备必须遵守生产契约，在执行 DW 回填或 CDC 前建立与 `DesiredSyncTable` 一致的 Meta 表和字段。
- R2：复用现有快照服务或测试工厂完成准备与清理，不新增仅服务于本次修复的生产抽象。
- R3：保留 `enqueue_value_refresh` 在 Meta 映射缺失时的失败行为；不得改为静默 no-op。
- R4：CI 继续运行 `integration` 测试；不得通过修改 pytest marker 表达式跳过失败用例。
- R5：测试结束后只清理本用例创建的 Meta、同步控制、outbox、DW 和源表数据，避免跨用例污染。
- R6：修复以 PR #71 当前 head 为实现基线，并在落地前重新确认远端 head，避免覆盖并发提交。

## Acceptance Criteria

- [x] `test_backfill_then_binlog_converges` 在真实 MySQL 测试环境中通过。
- [x] `test_json_sql_null_and_literal_null_remain_distinct_after_cdc` 在真实 MySQL 测试环境中通过。
- [x] 两条测试在首次调用 `apply_backfill_batch` / `apply_buffered_event` 前能够查到对应 Meta `column_info.table_id`。
- [x] 测试清理后不遗留本用例生成的 Meta 行或 metadata-index outbox 行。
- [x] `uv run pytest -m "not tei"` 通过，且没有减少 CI 原有测试范围。
- [x] Ruff、Pyright、配置加载、Compose 配置校验和 `git diff --check` 通过。
- [x] 生产代码中的缺失 Meta 守卫保持不变。

## Out of Scope

- 不处理 PR #71 中与本次 CI 失败无关的功能或 Codex review thread。
- 不新增 Qdrant、Elasticsearch 或 TEI CI service；当前失败发生在外部索引调用前。
- 不修改 `/codex-fix-ci`、review delegation 或 GitHub Token 配置。
