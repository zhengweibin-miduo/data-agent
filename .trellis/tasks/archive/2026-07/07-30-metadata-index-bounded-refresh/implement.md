# 实施计划

## Gate

本计划只有在设计评审通过后才进入实施。实施期间保持 PR #71 的原 head
`feature/metadata-semantic-value-index-20260730`，不新建 PR，不 force-push。

## Phase 1：数据库契约与迁移

1. 扩展 `metadata_index_outbox` 的 VALUES 工作状态，加入 `phase`、
   `progress_column_id`、`last_primary_key`、`bulk_cursor`、
   `frequency_version`、`pending_frequency_version`、`index_generation`、
   `lease_token` 所需约束。
2. 新增 `metadata_value_frequency`，建立精确唯一键和 Top-N 排序查询索引；
   `value_text` 最多对入选的 N 行回表读取，不宣称 TEXT 被二级索引覆盖。
3. 新增 `metadata_value_publication`，按真实 ES document ID 保存带版本的 desired
   membership、published 状态和可恢复的 publish/cleanup 动作日志。
4. 同步 SQLAlchemy Core `Table` 定义、MySQL fresh bootstrap 和数据库规范。
5. 为唯一键、索引和状态默认值补充 schema 测试。

验收：全新数据库 bootstrap 和测试 schema 一致；没有依赖全表
`GROUP BY` 或 `delete_by_query` 的兼容路径。

## Phase 2：有界精确频次模块

1. 增加动态 DW 表标识符验证和 Core `table()/column()` 构造器。
2. 实现按稳定主键范围读取一个有限批次，兼容单列及复合主键编码。
3. 在同一事务中聚合批内值、upsert 精确频次并推进 `last_primary_key`。
4. 实现频次增减、归零删除、负数保护和固定顺序锁。
5. 把一个批次封装为单一深接口，调用方只负责预算与调度。

验收：每次调用的 DW 读取行数和数据库写入量均有上限；进程在提交前后崩溃都能
重试，且频次不重不漏。

## Phase 3：CDC 与回填事务集成

1. 从现有共享 DW 目标映射出所有受影响的逻辑表及字段。
2. INSERT：新值 `+1`；DELETE：旧值 `-1`；UPDATE：仅变化字段执行旧值
   `-1`、新值 `+1`。
3. 复用现有 event/cursor 去重事务，不额外引入第二套事件幂等账本。
4. 扫描期按主键游标判断“已扫描区/未来区”，避免扫描和 CDC 双计数。
5. 统一锁顺序为值索引状态、DW 行、频次行，并按稳定键排序多目标锁。
6. 把频次变化、DW DML、事件确认/游标和 outbox enqueue 保持在同一事务。

验收：真实 MySQL 中重复投递、更新前后值、删除、共享目标和扫描竞态均保持精确。

## Phase 4：状态机与精确 Top-N

1. 实现 `SCAN -> SELECT_TOP_N -> PUBLISH -> CLEANUP -> COMPLETE`
   的单工作单元状态迁移。
2. `SELECT_TOP_N` 每次只处理一个字段，使用
   `frequency DESC, value_hash ASC` 的确定性排序。
3. 将 Top-N 行标记为当前 `desired_membership_version`；未入选旧行保留旧版本作为
   tombstone，全部字段物化完成后才允许做差集。
4. 在工作单元边界处理新 desired version：扫描期保留可复用频次，否则从
   `SELECT_TOP_N` 或 `SCAN` 安全重启。
5. 所有状态推进使用 lease/CAS，旧 worker 不能覆盖新 lease。

验收：每个 phase 可重复进入；超过一次 worker 预算后，后续任务能从持久化进度继续。

## Phase 5：Elasticsearch 差量发布

1. 文档 ID 固定为 `SHA256(table_id + NUL + column_id + NUL + value_hash)`。
2. `PUBLISH` 从 publication 表准备有限动作批，写入 ES 后以 CAS 结算。
3. `CLEANUP` 只删除 publication 中明确记录的过期 document ID，使用有界 bulk，不调用整表
   `delete_by_query`。
4. bulk 游标包含 phase/version/generation，动作保存 payload hash/body；未知远端
   结果通过稳定 ID 的幂等 index/delete 收敛，DELETE 404 成功，确定性 4xx 永久失败。
5. 搜索读取只暴露当前 `index_generation`，重建通过代际切换隔离旧发布集合。

验收：publish/cleanup 任意批次中断后可恢复，最终 ES 内容与精确 Top-N 一致。

## Phase 6：调用方、重建与兼容清理

1. worker 调用统一为 `MetadataValueRefresh.run_next_unit(claim, budget)`。
2. rebuild、后台任务和 API 只发 desired intent，不编排内部 phase。
3. 删除旧的无界 `GROUP BY` 投影与整表 `delete_by_query` 路径。
4. 明确修改 `value_projection_batch()`：不再用 `text(f"...")` 构造动态查询，
   与仓库 SQLAlchemy Core `Table` 规范一致。
5. 更新 `database-guidelines.md` 与 `code_review.md`，使规范和实际允许的
   Core/窄范围 `text()` 边界一致。

验收：仓库内不存在可达的旧无界扫描/清理实现，且规范检查不再与实现冲突。

## Phase 7：测试与验证

1. 单元测试：状态迁移、主键游标编码、频次 delta、Top-N 稳定排序、版本抢占、
   bulk 动作恢复和稳定文档 ID。
2. 真实 MySQL 集成测试：跨批恢复、CDC insert/update/delete、重复事件、共享目标、
   扫描竞态和事务回滚。
3. 真实 Elasticsearch 集成测试：发布中断、cleanup 中断、多批收敛和最终索引内容。
4. 组合故障测试：预算耗尽后继续、新 desired version 抢占、旧 lease 提交被拒绝。
5. 运行项目规定的格式、lint、类型、单元、集成和质量门禁。
6. 依据 `code_review.md` 做 Standards/Spec 双轴自审并记录未覆盖环境条件。

## Phase 8：提交与更新 PR

1. 复核 diff 仅包含本任务文件，确保没有带入其他 worktree 修改。
2. 按逻辑单元提交到原分支。
3. 首次推送前再次核验本地分支名、PR #71 head 和 remote tracking。
4. 使用普通 push 更新 PR #71；不创建新 PR，不 force-push。
5. 检查 PR checks 和最终 head SHA，汇报测试证据及残余风险。

## 回滚边界

- 数据库迁移采用向前兼容的新增列/表；回滚应用版本时保留新表，避免丢失恢复状态。
- 发布失败不切换 generation；旧可见 generation 保持可读。
- 频次版本只有完成扫描后才能进入发布；不把半成品频次暴露为 desired 集合。
- 若真实依赖服务不可用，相关集成测试必须明确标记环境阻塞，不能用 mock 结果替代通过。
