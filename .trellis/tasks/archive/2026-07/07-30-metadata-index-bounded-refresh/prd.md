# 让元数据值索引全流程有界且可恢复

## Goal

消除 PR #71 字段值索引在单字段聚合和 Elasticsearch 旧版本清理阶段仍可能超过
worker timeout、反复重做且无法收敛的问题。所有阶段都必须拆成有界、幂等、可恢复的
工作单元，并保持字段值频次与 Top-N 的精确语义。

## Background

- PR #71 当前 head 分支为
  `feature/metadata-semantic-value-index-20260730`，目标分支为 `master`。
- 当前逐字段刷新只在一个字段完整聚合并写入后推进 `progress_column_id`；
  单字段完整 `GROUP BY / ORDER BY` 和整表 `delete_by_query` 仍没有断点。
- `WorkerSettings.job_timeout` 使用
  `app_config.redis.worker_job_timeout_seconds`，当前配置为 600 秒。
- 当前功能尚未投入使用；本任务只保证 fresh bootstrap 和测试 schema 一致，不设计
  线上兼容迁移、旧索引接管或停写切换流程。
- 生产数据库访问规范要求使用 SQLAlchemy Core `Table` 与绑定参数；当前
  `value_projection_batch()` 的 `text(f"...")` 与规范冲突。

## Requirements

- R1：新增精确字段值频次汇总持久化模型，至少保存 `table_id`、`column_id`、
  `value_hash`、`value_text`、`frequency`，并提供保证同一字段同一规范化值唯一的
  唯一键以及支持精确 Top-N 的查询索引。
- R2：首次回填必须按稳定主键范围分批读取 DW 原始行，逐批累加精确频次并持久化
  `last_primary_key`；worker 重启、取消或租约丢失后从已提交游标继续。不得使用
  `GROUP BY LIMIT/OFFSET` 伪分页，也不得单纯延长 timeout。
- R3：现有 CDC 路径必须事务性、幂等地维护频次：INSERT 对新值 `+1`，DELETE 对
  旧值 `-1`，UPDATE 对旧值 `-1`、新值 `+1`；计数归零时删除或标记为不可发布。
  重复事件不得重复计数，共享 DW 目标不得把不同来源的事件或字段身份混淆。
- R4：每个字段的 Elasticsearch 候选必须从频次汇总表按精确
  `frequency DESC` 和确定性 tie-break 读取 Top-N，不采用 Space-Saving、
  Count-Min Sketch 或 Elasticsearch 近似 terms。
- R5：Elasticsearch 文档 ID 必须稳定派生自
  `table_id + column_id + value_hash`。系统必须持久化或可恢复地记录上一版已发布
  ID 集合，通过新旧集合差集生成新增、更新、删除操作，并按文档数与字节预算分批
  bulk；不得再使用整表同步 `delete_by_query`。
- R6：持久化刷新状态机覆盖 `SCAN`、`SELECT_TOP_N`、`PUBLISH`、`CLEANUP`、
  `COMPLETE`；保存
  `phase`、`progress_column_id`、
  `last_primary_key`、`bulk_cursor`、`desired_version`、`lease_token`。
  新 desired version 必须安全抢占旧版本；迟到 worker 不得推进或确认新状态。
- R7：每次 claim 只执行一个显式预算内的工作单元；每个单元必须幂等，数据库事务
  不跨 MySQL/Elasticsearch 远程 I/O，取消和崩溃后能够恢复。
- R8：修复 `value_projection_batch()` 动态 `text(f"...")` 与数据库规范冲突；
  采用 SQLAlchemy Core 动态 `Table`/`Column` 表达式或在设计评审中明确、更新一条
  等价且安全的仓库规范，禁止静默保留冲突。
- R9：fresh bootstrap、SQLAlchemy Core 表定义、模型和测试必须保持 schema parity；
  派生索引状态可从 DW 重新构建，不引入线上兼容迁移。
- R10：只更新 PR #71 原分支，不新建 PR，不 force-push。

## Acceptance Criteria

- [ ] AC1：大表首次回填跨多个 claim 推进；取消或 worker 重启后从已提交主键游标继续，
  已完成批次不重算且最终频次精确。
- [ ] AC2：CDC INSERT、DELETE、UPDATE 的频次变化正确，归零值不再进入 Top-N；
  重复投递不改变最终频次。
- [ ] AC3：共享 DW 目标的来源/字段身份规则与现有资格门禁一致，不发生跨来源误计数。
- [ ] AC4：Top-N 与权威 DW 行集的精确聚合结果一致，包含确定性并列排序。
- [ ] AC5：发布和 cleanup 中断后从已提交游标恢复；最终
  Elasticsearch 只包含当前 Top-N，且不执行整表同步 `delete_by_query`。
- [ ] AC6：新 desired version 在 `SCAN`、`SELECT_TOP_N`、`PUBLISH`、
  `CLEANUP` 任一阶段到达时均能抢占，旧 lease 的迟到写不会推进状态。
- [ ] AC7：故障测试证明工作量超过单次 worker 预算时仍能跨任务继续收敛。
- [ ] AC8：真实 MySQL + Elasticsearch 集成测试覆盖跨批恢复、CDC
  insert/update/delete、重复事件、发布中断、cleanup 中断、desired version
  抢占及最终索引内容。
- [ ] AC9：`uv lock --check`、Ruff、Pyright、compileall、配置校验、非 TEI
  pytest 全套、Docker Compose 配置和 `git diff --check` 全部通过。
- [ ] AC10：数据库查询实现与 `.trellis/spec/backend/database-guidelines.md`
  一致，`code_review.md` 中的既定事实没有被本改动静默破坏。
- [ ] AC11：提交、推送和 PR 更新均使用原 head 分支；推送前重新校验远端 head，
  不改写共享历史。

## Out of Scope

- 近似频次算法或近似 Top-N。
- 通过增加 worker、数据库或 Elasticsearch timeout 掩盖无界工作单元。
- 通用迁移框架、通用流处理平台或另建 PR。
- 尚未投入使用的旧 schema、旧文档 ID 和线上索引接管。
