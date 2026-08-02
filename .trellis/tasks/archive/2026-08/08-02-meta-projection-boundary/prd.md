# 重构 Accepted Snapshot 与 Meta Projection 边界

## Goal

落实 DDL Metadata 对 Meta Projection 的所有权，重塑 accepted snapshot publication seam，并替换对应测试。

## Background

- 父任务已在 `CONTEXT.md`、`CONTEXT-MAP.md` 中确认 Meta Projection 属于 DDL Metadata，Meta Snapshot 是权威来源。
- 当前 `MetadataSnapshotService` 在 `ddl_metadata.persistence` 内直接协调 Meta、Memory、Data Sync 与 Meta Projection repositories。
- `metadata_indexing` 的 pure desired policy、SQL、repositories、ES/Qdrant/TEI 和跨 context 表读取混合，projection interface 过宽。

## Requirements

- 为 accepted snapshot publication 定义深 module interface，保留 generation lock 覆盖单一 MySQL 事务的原子性。
- 将 Meta Projection 的 deterministic desired/version policies 与 persistence、ES/Qdrant/TEI adapters 分离。
- 定义 Data Sync 值输入/通知所需的稳定 port 或 projection event，供后续 Data Sync 子任务实现。
- DDL Metadata/Meta Projection 的 domain/application 不直接导入 Data Sync tables/repository implementation。
- 保持 Meta Snapshot authoritative、Qdrant/ES rebuildable、outbox convergence、generation/rebuild/retry 行为不变。
- 使用 Accepted Snapshot 与 Meta Projection 两类确认 seam 做 red-green-replace，不增加数据迁移。
- 硬迁移后不保留 `data_agent.metadata_indexing`；允许为保持现有运行时而最小改写 Data Sync 外层调用点，使其只调用新的公共 projection input，不在本任务内重构 Data Sync application ports。

## Acceptance Criteria

- [ ] accepted snapshot publication 通过明确 interface 原子提交 Meta、Memory、Data Sync desired 与 projection outbox，失败完整回滚。
- [ ] Meta Projection domain/application 不再读取 `data_sync.tables` 或其他 context 的 persistence tables。
- [ ] projection input interface 足以让 Data Sync 子任务移除对 Meta Projection implementation 的反向导入。
- [ ] Meta Projection 的权威/派生关系、outbox convergence 和搜索 authoritative readback 保持不变。
- [ ] 新 seam 测试完成 red-green，被覆盖的私有 `_synchronize`/helper 和重复 generation/cursor 测试已删除。
- [ ] 活动代码与测试不再导入 `data_agent.metadata_indexing`；Data Sync 仅通过新的公共 projection input 维持当前行为。
- [ ] 相关静态检查、非集成 pytest 与 snapshot/index 集成测试通过；不可用服务如实记录。

## Out of Scope

- 设计和实现 Data Sync application ports、用例注入与内部模块重构；由依赖子任务完成。本任务只允许完成硬包迁移所必需的外层调用点适配。
- 外部 HTTP/SSE 契约或数据库 schema 迁移。
