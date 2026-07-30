# 当前实现证据

## 1. 刷新状态

- `src/data_agent/metadata_indexing/tables.py:8-37` 的
  `metadata_index_outbox` 只保存 `desired_version`、
  `pending_desired_version`、`progress_column_id`、租约、重试和错误。
  它无法表示字段内主键游标、发布游标或刷新阶段。
- `src/data_agent/metadata_indexing/repository.py:41-144` 在活跃值刷新收到新版本时
  保留当前版本和游标，把新版本放入 `pending_desired_version`。
- `src/data_agent/metadata_indexing/repository.py:146-269` 已有
  `FOR UPDATE SKIP LOCKED`、数据库时钟租约和
  `desired_version + lease_token` authority CAS，可作为新状态机的并发基础。
- `src/data_agent/metadata_indexing/dispatcher.py:178-257` 每次领取只处理一个字段；
  `progress_column_id` 只有在整个字段完成后才提交。

## 2. 无界工作

- `src/data_agent/metadata_indexing/projections.py:361-396` 对一个完整 DW 字段执行
  `GROUP BY / ORDER BY / LIMIT`。`LIMIT` 只限制返回结果，不能限制聚合扫描成本。
- `src/data_agent/metadata_indexing/elasticsearch.py:143-166` 使用一次
  `delete_by_query` 清理整表旧代次，只有调用前后的 heartbeat，没有恢复游标。
- `src/data_agent/ddl_metadata/worker/settings.py:111` 让维护 cron 继承统一 worker
  timeout；当前配置是 600 秒。

## 3. 可复用的有界机制

- `src/data_agent/data_sync/backfill.py:31-64` 已实现按简单或复合稳定主键的 keyset
  batch；无主键和不可靠排序主键在模型验证阶段拒绝。
- `src/data_agent/data_sync/backfill.py:67-97` 把 DW batch、主键游标和刷新 desired
  state 放在同一事务。
- `src/data_agent/data_sync/backfill.py:174-231` 在一个事务中应用 CDC
  before/after、确认事件、推进位点和 enqueue；UPDATE 主键变化按旧行 DELETE 与
  新行 INSERT 收敛。
- `src/data_agent/data_sync/tables.py:71-96` 的事件坐标唯一键和
  `repository.py:588-701` 的 `INSERT IGNORE + acknowledged_at` 使重复捕获和事务
  重试幂等。
- `src/data_agent/data_sync/tables.py:99-115` 的 key owner 以
  `(target_table, primary_key_hash)` 固定共享 DW 目标的来源归属。
- `src/data_agent/metadata_indexing/elasticsearch.py:242-283` 已有每批 500 文档、
  5 MiB 的双预算 bulk 分块。

## 4. 需要修正的既有设计

- 归档设计
  `.trellis/tasks/archive/2026-07/07-30-metadata-semantic-value-index/design.md:130-143`
  选择每次从 DW 快照重算 Top-N。
- 后续归档设计
  `.trellis/tasks/archive/2026-07/07-30-metadata-index-resumable-refresh/design.md:15-20`
  明确拒绝逐行频次计数器。
- 当前审查证明“一个字段”仍不是有界工作单元；本任务按用户明确要求用可恢复的
  精确频次汇总取代全字段聚合。原决策由本任务设计显式 supersede，不静默漂移。

## 5. SQLAlchemy 规范

- `.trellis/spec/backend/database-guidelines.md:241-255` 把生产持久化描述为静态
  SQLAlchemy Core `Table`，不允许插值模型标识符。
- 当前 `value_projection_batch()` 使用 `text(f"...")`；同时 data-sync 的动态
  source/DW schema 路径也存在经 dialect quote 的动态 SQL。
- 推荐决策：项目拥有的控制表继续使用静态 `Table`；经 `DesiredSyncTable`
  验证的动态 source/DW 表使用 SQLAlchemy Core `table()` / `column()` 和绑定值。
  只有 Core 无对应表达的 MySQL 控制语句可用窄化 `text()`，不得拼接未验证标识符。
