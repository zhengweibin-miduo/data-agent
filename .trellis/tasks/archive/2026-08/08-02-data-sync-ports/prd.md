# 重构 Data Sync 应用端口

## Goal

为 Data Sync 建立 application ports，消除与 Meta Projection 的双向 implementation 依赖，并替换对应测试。

## Background

- `DataSyncService` 当前直接导入 backfill、binlog、repository、schema synchronizer 与 `MySQLDatabase`。
- `data_sync.backfill → metadata_indexing.value_refresh → data_sync.models/tables` 构成结构性双向依赖。
- 本任务依赖 `08-02-meta-projection-boundary` 先确定并提供 reviewed Meta Projection input interface。

## Requirements

- 为 task repository/unit-of-work、source reader、DW writer/schema adapter、lease/clock 和 Meta Projection notification/input 定义真实 application ports。
- composition root 选择 MySQL/source/schema/projection adapters；Data Sync application 不直接导入具体 infrastructure。
- 删除 `data_sync` 对 `metadata_indexing` implementation 的直接导入，改用已评审的 projection interface/event。
- 保持 task phase、captured/applied coordinates、backfill/replay/streaming、readiness、retry/backoff 与 lease invariants。
- 测试统一从 `dispatch_once` 或明确外部 adapter contract 进入；新 interface 覆盖后删除 `_process/_capture/_retry/_reschedule` 测试。
- 不增加数据库或历史数据迁移。

## Acceptance Criteria

- [x] Data Sync application modules 不再直接创建 MySQL session 或依赖具体 source/schema/projection implementations。
- [x] `data_sync → metadata_indexing` implementation 导入为零，结构性双向依赖消失。
- [x] `dispatch_once` seam 可用 in-memory adapters 证明 phase、coordinate、DW 和 lease/error 行为。
- [x] 新 seam 覆盖后，私有 lifecycle/call-order 测试被删除且没有降低已确认回归保护。
- [x] CDC 与 DW convergence 的现有集成场景保持通过。
- [x] 相关 Ruff、Pyright、compileall、非集成 pytest 与 Data Sync 集成测试通过；不可用服务如实记录。

## Dependency

- 在 `08-02-meta-projection-boundary` 的 projection interface 评审完成前，不得激活本任务实现。

## Out of Scope

- 重新定义 Meta Projection 的领域归属或修改前端。
- 数据库 schema、共享开发卷或历史数据迁移。
