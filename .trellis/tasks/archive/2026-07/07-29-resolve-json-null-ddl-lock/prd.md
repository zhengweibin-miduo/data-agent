# 修复 JSON 空值语义与 DDL generation 串行化

## Goal

修复 PR #66 中仍未闭环的两个 P1 数据同步缺陷：

1. 保留 MySQL JSON 列中 SQL `NULL` 与 JSON literal `null` 的不同语义，确保 CDC 回放与源库一致。
2. 使用共享串行锁消除 generation authority 校验与不可逆 MySQL DDL 生效之间的竞态。

修复完成并通过验证后，将提交普通推送到 PR #66 的原 head 分支，在两个原 review thread 中回复实际提交和验证依据，并 resolve。

## Background

- PR #66 当前 head 为 `111aff143478b4338b89c1218b8a659a0de4bec1`，目标分支为 `master`。
- JSON review thread：`discussion_r3671097004`，当前锚点为
  `src/data_agent/data_sync/binlog.py:322`。
- generation review thread：`discussion_r3673800168`，当前锚点为
  `src/data_agent/data_sync/schema_sync.py:103-105`。
- `mysql-replication==1.0.16` 在 `RowsEvent._read_column_data()` 中消费
  null bitmap；SQL `NULL` 与 JSON binary literal `null` 最终都表现为
  Python `None`，现有应用层编码无法可靠猜测来源。
- `MetadataSnapshotService.persist()` 通过一个 Meta MySQL 事务发布新的
  desired hash；Worker 当前在另一个 Session 中检查 authority，再在 DW
  Session 中执行自动提交 DDL，两者之间存在不可封闭的检查-执行窗口。
- Meta、`data_sync` 与 `dw` 使用同一个 `MySQLDatabase` 引擎和 MySQL
  实例，因此可以使用同一 MySQL advisory-lock 命名空间协调发布者与 Worker。

## Requirements

- R1：CDC 必须在第三方 ROW 事件的 lazy row decode 边界保留 JSON 列
  SQL `NULL` 的来源，不能在值已退化为 `None` 后猜测。
- R2：JSON literal `null` 继续使用现有 `{"$json":"null"}` 可逆编码；
  SQL `NULL` 必须继续使用普通 `None`，不得改变现有 durable event JSON
  契约。
- R3：INSERT、UPDATE 的 before/after image 与 DELETE image 均使用同一
  空值区分逻辑；非 JSON 列和非空 JSON 值行为保持兼容。
- R4：不得 fork、vendor 或修改虚拟环境中的第三方包；兼容适配必须由
  `data_agent.data_sync` 所有，并在锁定依赖版本不兼容时明确失败。
- R5：为每个 DW target 生成稳定、无碰撞且不超过 MySQL 64 字节限制的
  generation lock 名称；同一 target 的所有来源共享同一锁。
- R6：`MetadataSnapshotService.persist()` 必须在持有全部相关 generation
  locks 时提交 Meta、memory、outbox 与 desired-state 事务，锁只能在 commit
  或 rollback 完成后释放。
- R7：Worker 必须先取得同一 generation lock，再取得现有 DW schema lock；
  在锁内用执行 DDL 的同一 Session 重新校验 task desired hash、lease token
  与租约有效期，随后才允许执行自动提交 DDL。
- R8：锁竞争是可恢复的调度压力：Worker 不消耗失败预算；Meta 发布方以
  可重试安全错误退出，不得无限等待。
- R9：锁必须按稳定顺序获取、逆序释放；部分获取失败、业务异常、commit
  失败和取消都不得把 advisory lock 遗留在连接池连接上。
- R10：不得持有 generation lock 进行源库访问、Binlog 捕获、历史回填或
  LLM 调用；锁只覆盖 accepted snapshot 的数据库提交与 Worker 的结构同步。
- R11：保持现有公开 API、任务阶段、durable 数据表、Binlog 位点、DW 行和
  DDL Job 成功边界兼容。
- R12：只有代码修复、要求的验证和普通推送都成功后，才能在原 thread
  回复并 resolve；任一步失败时保持 unresolved。

## Acceptance Criteria

- [ ] AC1：真实 MySQL JSON 列分别写入 SQL `NULL` 与 JSON literal `null`
  后，经 Binlog 捕获、durable codec 和 DW 回放仍可通过 `IS NULL` 等查询
  区分。
- [ ] AC2：JSON 对象、数组、标量、literal `null`、非 JSON SQL `NULL`
  以及 INSERT/UPDATE/DELETE 的既有单元测试继续通过。
- [ ] AC3：当旧 Worker 先持有 generation lock 时，新 snapshot 发布必须
  在配置的有限等待时间内保持未提交；旧 DDL 与旧 phase settlement 完成后
  才能提交新 generation，超过等待预算则返回可重试错误而不是无限阻塞。
- [ ] AC4：当新 snapshot 先提交时，旧 Worker 取得锁后重新校验 authority
  失败，且不会执行任何旧 DDL。
- [ ] AC5：同一 target 的结构同步串行执行，不同 target 仍可并行；锁超时、
  异常和取消路径均释放已取得的锁。
- [ ] AC6：`uv lock --check`、Ruff、Pyright、compileall、settings 加载、
  非 integration pytest、相关 MySQL integration、Compose config 和
  `git diff --check` 均通过，或对不可用的外部服务如实报告。
- [ ] AC7：改动普通推送到
  `feature/llm-data-sync-status-tool-20260727`，远端 head 与实际提交一致；
  两个指定 review threads 都包含提交和验证依据并处于 resolved。

## Out of Scope

- 不引入 Kafka、Debezium、影子表切换或分布式事务。
- 不改变其他 generation reset、回填、CDC 缓冲或 readiness 逻辑。
- 不新增 `binlog_row_image=MINIMAL` 或 `PARTIAL_UPDATE_ROWS_EVENT` 支持；
  运行时继续要求 FULL row image，本任务验证当前已接受的
  `WriteRowsEvent`、`UpdateRowsEvent` 与 `DeleteRowsEvent`。
- 不升级 `mysql-replication`，除非实现过程中证明锁定版本无法应用已设计的
  事件实例适配器；发生该情况必须回到规划阶段。
- 不 resolve 其他 review thread，也不创建新 PR。
