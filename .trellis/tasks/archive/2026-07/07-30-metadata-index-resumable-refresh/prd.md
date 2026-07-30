# 修复元数据索引长刷新无法收敛

## Goal

让字段值索引刷新在单张表工作量超过 arq 单次 600 秒执行上限、worker 重启或任务取消后仍能继续推进并最终收敛，避免每次从第一个字段重算而永久保持不完整。

## Background

- PR #71 当前唯一 unresolved 审查项位于 `src/data_agent/ddl_metadata/worker/settings.py:80-82`。
- metadata index cron 没有独立 timeout，因而继承 `WorkerSettings.job_timeout=600`。
- 一次 values 刷新会逐个处理表内全部 eligible 字段；每字段最多聚合 10,000 个高频值并分批写入 Elasticsearch。
- 当前 outbox 只持久化表级 desired state、租约和重试信息，不持久化字段或分块进度；任务取消后租约到期，再次领取会从首字段开始。
- Elasticsearch 旧版本清理必须在对应刷新范围完整写入后执行，不能因分块或恢复而误删仍有效数据。

## Requirements

- R1：长刷新必须拆成可在单次 worker timeout 内完成的有界工作单元。
- R2：每个已完成工作单元的进度必须持久化；取消、超时、进程退出或租约转移后不得从已确认的首个工作单元重新开始。
- R3：stale worker、迟到完成和 desired version 更新不得确认或覆盖更新版本的进度。
- R4：只有当前刷新范围的全部工作单元成功后，才能完成旧 refresh version 清理并确认表级 desired state。
- R5：部分完成期间允许新旧 refresh version 暂时并存，但检索完整性必须保持 `complete=false`，且恢复后最终只保留当前版本。
- R6：Meta、DW 和另一索引目标仍与字段值索引失败隔离；刷新失败不得回滚已经提交的权威数据。
- R7：保留现有字段 Top-N、规范化、批量字节预算、租约和 dead-letter 契约，除非实现恢复语义确实需要最小调整。
- R8：已有环境的数据库升级必须是精确、可审查的 schema 变更；运行时不得自动改表。

## Acceptance Criteria

- [x] 单张表刷新总耗时可超过 600 秒，但每个调度工作单元有界并能跨多次执行最终确认。
- [x] 在至少一个字段/分块完成后模拟 `CancelledError` 或进程退出，下一次执行从持久化断点继续，而不是重做已确认工作。
- [x] 旧 worker 在 desired version 或 lease token 变化后，不能提交进度、清理旧版本或确认任务。
- [x] 最后一个工作单元成功前 outbox 保持 pending 且 `complete=false`；成功后旧版本被清理、outbox 被确认且 `complete=true`。
- [x] 中途失败或取消不会误删旧索引；恢复完成后不会遗留旧 refresh version 文档。
- [x] 新 desired version 在旧刷新进行中到达时，旧进度安全失效，新版本可以从一致状态开始并最终收敛。
- [x] 单元测试覆盖超时恢复、迟到 worker、新 desired version、最终清理和完整性状态；相关现有非集成测试继续通过。
- [x] 具备真实 MySQL + Elasticsearch 的集成验证，覆盖跨任务恢复与最终索引内容；无法在本机执行时必须明确记录未验证原因。

## Out of Scope

- 不改变 Meta 或 DW 的提交成功语义。
- 不新增 HTTP、公共 DTO、问数入口或新的索引产品能力。
- 不为理论吞吐量提前引入与恢复正确性无关的通用任务编排框架。

## Notes

- 本任务从 PR #71 head `5de3942a13c492534c24ceccc2ecfe99c211de49` 建立独立分支。
- 这是复杂状态机修改；进入实现前必须补齐 `design.md`、`implement.md`、上下文 manifests 并通过规划评审。
