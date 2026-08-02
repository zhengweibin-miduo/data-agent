# Data Sync / Metadata Indexing architecture mapping

## Scope and architecture

- **事实**：后端是同一 Python 模块化单体；两个 bounded context 分目录实现：`data_sync/` 负责 DW 结构、回填和 Binlog 状态机，`metadata_indexing/` 负责 Meta desired-state/outbox 与外部索引投影。`DataSyncService` 只依赖 `data_sync` ports/repository 与 MySQL/source adapter（`src/data_agent/data_sync/service.py:13-32`）；`MetadataIndexDispatcher` 只编排 metadata outbox、projection、Qdrant/Elasticsearch/TEI adapters（`src/data_agent/metadata_indexing/dispatcher.py:5-28`）。
- **判断**：上下文在实现上基本独立；唯一明确的协作 seam 是 accepted snapshot 发布事务把 Data Sync desired state 与 Metadata Index desired state 一起提交，以及 metadata value refresh 读取 `data_sync_task` 作为共享 DW peer 契约（见下文）。未发现一个 context 直接调用另一个 context 的 service/application 实现；存在跨 context 的表/模型读取，属于共享持久化 seam，应视为防腐层/契约边界而非独立性完全隔离。

## End-to-end flow: accepted snapshot -> desired state

1. `AcceptedSnapshotPersistence.persist()` 先构建 `DesiredSyncTable`（`snapshots.py:106-111`），计算每个目标的 generation lock（`snapshots.py:112-120`），在 advisory locks 内开启唯一 MySQL session 事务（`snapshots.py:117-124`）。
2. 同一事务先同步 Meta snapshot，然后 `DataSyncRepository.upsert_desired(desired_tables)` 发布 durable Data Sync handoff（`snapshots.py:143-157`）。`upsert_desired` 对目标冲突做校验并按 desired hash 重置旧 generation：删除 `data_sync_event`、清空游标/位点并置 `pending_schema`，首次则插入任务（`data_sync/repository.py:76-87`, `131-179`）。
3. 之后在相同事务生成 `semantic_desired_states`、补充 `shared_value_refresh_states`，并调用 `MetadataIndexOutboxRepository.enqueue`（`snapshots.py:158-197`）。因此表/事务拥有者是 `ddl_metadata.persistence.snapshots` 的发布事务；两个 context 的控制表由各自 repository 写入，但共享一次原子提交。

## Data Sync flow: desired -> schema/binlog/backfill/replay/DW

- **控制表/所有权**：`data_sync_task` 保存 source/target identity、desired JSON/hash、phase、snapshot/captured/applied coordinates、backfill key、lease/error 状态；`data_sync_event` 保存去重 Binlog 行事件；`data_sync_key_owner` 保存 DW target primary-key ownership（`data_sync/tables.py:21-117`）。`DataSyncRepository` 明确“在调用方事务内维护数据同步控制状态”（`data_sync/repository.py:69-74`）。
- **入口/租约**：`dispatch_once()` 短事务 `claim_tasks(limit=1)`，随后逐任务 `_process_safely`（`data_sync/service.py:53-63`）。`PENDING_SCHEMA` 检查 source SELECT 权限并进入 `DWSchemaSynchronizer`（`service.py:105-120`）；schema synchronize 取得 target generation advisory lock，在 DDL session（READ COMMITTED）和 provenance snapshot（REPEATABLE READ）内执行，再结算 `BUFFERING`（`service.py:261-298`）。
- **Binlog 基线**：`BUFFERING` 读取 source current coordinate，`_reset_generation` 分批清理旧 DW rows，记录 snapshot/captured coordinate，切换 `BACKFILLING`（`service.py:121-128`, `236-259`）。
- **捕获与回填**：`BACKFILLING` 先 `_capture` 从 `captured or snapshot` 起点读取 source Binlog，写 `data_sync_event` 并推进 captured coordinate（`service.py:300-331`）；有容量时 `read_backfill_batch` 按主键游标读取源 rows，`apply_backfill_batch` 在 MySQL session 写 DW 并推进 backfill key，行耗尽后转 `REPLAYING`（`service.py:158-186`）。缓冲饱和时逐事件 `apply_buffered_event` 后 cleanup（`service.py:129-157`）。
- **回放/流式**：`REPLAYING`/`STREAMING` 读取并应用一个 buffered event；清空后 replay 转 streaming，streaming 轮询 source capture，所有阶段通过 lease heartbeat/短 renewal 避免长事务持锁（`service.py:188-234`, `346-386`）。
- **DDD 角色映射**：`data_sync.models` 的 `DesiredSyncTable`, `BinlogCoordinate`, `SyncPhase`, `SyncRowEvent` 是 domain data/状态；`DataSyncService` 是 application orchestration/state machine；`DataSyncRepository`、`MySQLSourceClient`、`DWSchemaSynchronizer`、`backfill` 是 driven adapters/ports 实现；`data_sync.tables` 是 persistence adapter schema。未见 domain 层依赖 FastAPI/ES/Qdrant。

## Metadata Indexing flow: desired -> outbox -> projections -> ES/Qdrant

- **Desired generation**：`semantic_desired_states` 将 physical+semantic table/column/metric payload 规范化为 `MetadataIndexDesired` UPSERT/DELETE；每个状态以 schema fingerprint、projection version 和对象 payload 计算稳定 version；VALUES refresh 按 table eligibility 生成 frequency version（`metadata_indexing/desired.py:58-206`）。`shared_value_refresh_states` 从 `data_sync_task.desired_json` 聚合同一 DW target 的 peers（`desired.py:209-220`），这是跨 context 的明确读取 seam。
- **Outbox ownership**：`metadata_index_outbox` 复合主键为 target/object_kind/object_id，保存 operation、desired/pending versions、VALUES phase/cursors/index generation、lease/attempts（`metadata_indexing/tables.py:19-55`）；`metadata_value_frequency` 与 `metadata_value_publication` 保存字段频次和发布代次（`tables.py:57-133`）。`MetadataIndexOutboxRepository` 在调用方事务内合并、领取和结算（`repository.py:38-43`）；enqueue 对 semantic 批量 upsert，对 values 行锁合并 pending version（`repository.py:45-61`, `146-229`）。
- **Dispatcher/seam**：`MetadataIndexDispatcher.dispatch()` 先短事务 claim/lease，之后不持 MySQL 事务调用外部服务；每项以 metadata lock + 全局 rebuild lock 串行化，并重新 renew lease 确认 desired 仍权威（`dispatcher.py:35-64`）。
- **Qdrant semantic path**：semantic UPSERT/DELETE 由 `_synchronize_semantic` 读取 MySQL `MetadataProjectionRepository` 权威 projection，TEI 生成 embedding，再 `MetadataQdrantIndex.upsert/delete`；写后重读 projection fingerprint，只有 fingerprint 与 desired 写入一致才 acknowledge，否则 restore reconciliation（`dispatcher.py:109-162`）。Qdrant 仅承载可重建语义投影，不是权威 Meta 状态。
- **Elasticsearch values path**：VALUES refresh 委托 `MetadataValueRefresh.run_next_unit`（`dispatcher.py:164-177`）；该模块使用 `MetadataValueElasticsearchIndex` + infrastructure Elasticsearch client（`value_refresh.py:37-40`, `1401-1454`），频次/发布状态先落 MySQL tables，再按 bounded bulk 单元发布/cleanup 到 ES。`search.py` 明确 Qdrant 只提供有序对象身份、ES 只提供解析字段范围候选，权威内容回读 Meta projection（`metadata_indexing/search.py:76-112`）。
- **DDD 角色映射**：`metadata_indexing.models`/desired version functions 是 domain contract/value state；`MetadataIndexDispatcher` 是 application orchestrator；`MetadataIndexOutboxRepository`、`MetadataProjectionRepository`、`MetadataValueRefresh` 是 driven adapters/application services；`MetadataQdrantIndex`/`MetadataValueElasticsearchIndex`/TEI clients 是 external adapters；`metadata_indexing.tables` 是 persistence schema。

## Boundary, depth, leverage, locality

- **module/depth**：两个 context 都有较深模块：Data Sync 的 service 将 lease、schema lock、binlog capture、backfill、replay 分阶段隔离；Metadata Indexing 将 desired calculation、outbox state machine、projection reads、ES/Qdrant writes 分离。Dispatcher 明确不把远程调用放在数据库事务内（`dispatcher.py:38-45`），是高 locality 的 orchestration seam。
- **interface/seam**：稳定接口是 `DataSyncRepository.upsert_desired/claim_tasks/read_events`、`MetadataIndexOutboxRepository.enqueue/claim/acknowledge` 与外部 index adapter 方法；跨 context seam 是 `shared_value_refresh_states` 直接 select `data_sync_task.desired_json`（`desired.py:209-220`）以及同一 accepted snapshot transaction 同时调用两个 repository（`snapshots.py:155-197`）。
- **leverage**：generation advisory locks 统一保护 DW target 发布、metadata semantic/value refresh 和 rebuild（`snapshots.py:112-124`; `dispatcher.py:47-55`），让并发收敛、迟到 worker 拒绝和可重建索引共享同一并发控制策略。Data Sync 的 `data_sync_key_owner` 将跨 source 主键冲突变成显式持久化归属，阻止隐式覆盖（`data_sync/tables.py:99-117`）。
- **直接共享 implementation**：未发现 `MetadataIndexDispatcher` 直接调用 `DataSyncService` 或反向调用；但 metadata desired 读取 Data Sync table/model（`desired.py:6-8`, `217-220`），属于表级共享实现，耦合点应在演进时通过显式 projection/port 约束。ES/Qdrant clients 只在 metadata indexing adapters（及 memory indexing 的另一 context）中使用，Data Sync service 不依赖它们。

## 不确定项

- 本报告追踪了 accepted snapshot、Data Sync worker 与 Metadata Index dispatcher 的核心链路；未展开应用启动时 worker 调度注册、DDL schema synchronizer 内部 SQL 细节及 `MetadataValueRefresh` 的每个 phase 实现。上述边界判断基于当前源码调用关系和表定义。
