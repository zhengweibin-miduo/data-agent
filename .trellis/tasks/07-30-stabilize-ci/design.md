# CI 失败修复设计

## 根因与边界

`enqueue_value_refresh` 根据同步字段 ID 查询 Meta `column_info`，以 Meta `table_id` 作为字段值索引刷新对象。没有映射时抛错是生产一致性守卫，防止 DW 已写入但字段值索引刷新被静默丢弃。

生产路径通过 `MetadataSnapshotService.persist` 先写 Meta，再写 `data_sync_task`。两条失败测试绕过该入口，只手工写同步任务，因此测试夹具不再满足新增的生产前置条件。

## 最小修复

只修改 `tests/integration/data_sync/test_cdc_pipeline.py` 及必要的既有测试 helper：

1. 为每条失败测试构造与现有 `DesiredSyncTable` 使用相同表 ID、字段 ID 的物理/语义快照。
2. 通过现有 `MetadataSnapshotService.persist` 或现有工厂写入 Meta 和同步 desired state，避免测试复制生产 SQL。
3. 保留现有 DW 回填、Binlog、JSON null 断言。
4. 在 `finally` 中复用作用域清理 helper 删除本用例 Meta 与 outbox，再删除既有源表、DW 表和同步控制数据。

如果现有快照 helper 会改变测试要验证的同步任务领取顺序，则采用已有 repository 的最小 Meta seed helper；不新增生产 API。

## 兼容与回滚

- 不改生产代码、数据库 schema 或 CI workflow。
- 回滚仅需撤销测试夹具改动。
- 修复不得把缺失 Meta 映射降级为 no-op，也不得从 CI 排除 integration marker。

## 交付分支

实现基线应为 PR #71 当前 head `feature/metadata-semantic-value-index-20260730`。本 Trellis worktree 保持独立任务分支；实现前先快进到经核验的 PR head，交付时仅把本次修复提交安全地落到现有 PR 分支，不创建重复功能 PR。
