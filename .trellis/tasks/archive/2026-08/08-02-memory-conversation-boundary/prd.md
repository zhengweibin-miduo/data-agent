# 重构 Memory 与 Conversation 边界

## Goal

建立 Memory/Conversation application ports，消除具体 persistence/infrastructure 依赖，并按确认 seam 替换测试。

## Background

- 父任务审查证据位于 `.trellis/tasks/08-02-align-project-structure-tests/research/import-graph-audit.md` 与 `memory-conversation-flow.md`。
- `memory.application` 和 `conversation.service` 当前直接依赖 infrastructure、具体 repositories 与全局配置。
- Conversation 的 user-data deletion、context recall 和 extraction 直接调用 Memory persistence implementation，缺少 application port/防腐层。

## Requirements

- 为 Memory 与 Conversation 用例定义真实 application ports；只有存在生产 adapter 与 in-memory/test adapter 或真实变化点时才建立 seam。
- 把 MySQL session/repository、ES/Qdrant/TEI 和配置解析移到 adapters/infrastructure 或 composition root。
- Conversation 仅通过 Long-term Memory application interface 执行 recall、mutation/extraction proposal 与 user-data deletion 协作。
- 保持 Conversation turn/outbox/idempotency/tenant isolation，以及 Long-term Memory authoritative MySQL、history、tombstone-before-purge 和 rebuildable projections 不变。
- 使用确认的 Conversation/Long-term Memory seam 做纵向 red-green-replace；新 interface 覆盖后删除私有 helper 或 collaborator-interaction 测试。
- 不增加数据库、向量索引或历史数据迁移路径。

## Acceptance Criteria

- [ ] 目标 application modules 不再导入具体 infrastructure client 或自行创建 MySQL session/repository。
- [ ] Conversation 不再导入 `memory.mysql`，跨 context 协作只经过明确的 Long-term Memory application interface。
- [ ] Conversation 与 Long-term Memory 的现有外部契约、事务不变量和权威状态语义保持不变。
- [ ] 新 seam 测试完成真实 red-green 证明，被替代的实现耦合测试已删除且覆盖没有重复叠加。
- [ ] 相关 Ruff、Pyright、compileall、非集成 pytest 及 Conversation/Memory 集成测试通过；不可用服务如实记录。

## Out of Scope

- DDL accepted snapshot、Meta Projection、Data Sync 和前端 Workbench 重构。
- 数据库 schema 或历史数据迁移。
