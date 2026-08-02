# DDL metadata 请求流与数据流（Phase 2 取证）

## 最短调用链

`POST /api/v1/metadata/ddl-jobs` (`ddl_metadata/api/jobs.py:24-55`, `submit_job`) → `DDLJobStore.submit` (`jobs/store.py:102-151`) → Redis Lua/state stores (`jobs/redis/state_store.py:38-46`) 写入 Job Hash、source lease、dispatch outbox → 可选 `dispatch_one` (`jobs/store.py:64-77`) 或 worker cron `dispatch_pending` (`worker/maintenance.py:24-29`) → arq `run_ddl_job` (`worker/job_runner.py:217-227`) → LangGraph `graph.astream(..., stream_mode="tasks", durability="sync", version="v2")` (`worker/job_runner.py:385-397`) → graph nodes (`workflow/graph.py:43-95`) → `persist_node` 唯一写出口 (`workflow/nodes.py:472-515`) → `MetadataSnapshotService.persist` (`persistence/snapshots.py:61-70`) 在 MySQL 单事务同步 Meta、Memory 与索引 outbox (`snapshots.py:117-200`) → worker `_project_snapshot` 将 checkpoint 终态映射回 Redis Job 状态 (`worker/job_runner.py:153-214`) → GET 轮询或 SSE (`api/jobs.py:58-92`, `api/job_events.py:39-97`)。

## 入口、校验与受理

- FastAPI 组合根在 `application.py:191-208` 注册 `ddl_metadata_router`；生命周期初始化 Redis/MySQL/LLM/队列并注入 `app.state.jobs = DDLJobStore(redis, queue)` (`application.py:43-72`)。这是 composition root/infrastructure adapter。
- `submit_job` 通过 Pydantic `DDLJobRequest`（`api/jobs.py:29-37`）和 UUID `Idempotency-Key` header（`32-36`）完成 HTTP 边界校验；body header 覆盖 `submission_id`（`43-47`）。
- `DDLJobStore.submit` 先按 UTF-8 字节上限拒绝 DDL（`jobs/store.py:108-119`），随后以 request submission_id 或 uuid 建立 Redis 权威状态（`120-137`），回读安全公开投影（`138-151`）。同一逻辑 source 的租约/幂等冲突由 `source_busy`/`submission_conflict` 返回，原始 DDL 不进入响应。
- 角色：API 是输入 adapter；`DDLJobStore` 是 application lifecycle facade；Redis 专职 stores 是 driven adapter；Lua 状态脚本是原子 seam。业务状态规则集中在 store，未见独立 domain service。

## Redis 调度、worker 与 checkpoint

- 受理成功后 `_activate_now_safely` 尝试 `JobOutboxStore.dispatch_one`；失败保留 outbox 由 cron 重放（`jobs/store.py:64-77`, `worker/maintenance.py:24-29`）。outbox 将 `run_ddl_job(job_id, revision)` 入 arq（`jobs/redis/outbox_store.py:20-45`）。
- `run_ddl_job` 读取公开记录和 graph version，检查终态/revision，heartbeat/续租并 CAS 进入 RUNNING（`worker/job_runner.py:228-339`）；随后固定 `thread_id=job_id`（`341-357`），无 checkpoint 注入 execution_input，interrupt 则读取 stored answers 以 `Command(resume=...)` 恢复（`347-383`）。
- LangGraph checkpointer 由 worker lifecycle 初始化：`CheckpointStore.initialize()` 并传给 `build_ddl_metadata_graph(..., checkpointer)`（`worker/lifecycle.py:122-137`）；基础设施实现是 `infrastructure/checkpoint_store.py:5-24` 的 `AsyncRedisSaver`，属于 infrastructure adapter。
- `astream` 仅消费 tasks 事件并通过 `_task_start_stage` 映射稳定公开阶段，发布 `jobs.publish_progress`；节点 input/output/interrupt 不越过公开事件边界（`job_runner.py:385-397`）。异常按 `DataAgentError.retryable`/瞬态基础设施错误分类，重试或写 FAILED（`job_runner.py:409-458`）。

## LangGraph、memory context 与领域校验

- `build_ddl_metadata_graph` 注册 parse → memory load → classify → validate → question planning/interrupt → metric generation/validation → memory candidates → persist；只有 `persist_snapshot` 到 END（`workflow/graph.py:43-95`）。图是 application orchestration seam，节点依赖通过 `DDLGraphDependencies` 注入（`workflow/contracts.py:85-95`）。
- `MemoryContextLoader.load` 依据当前 `PhysicalSchema` 的 scope fingerprints 查询 MySQL `MemoryRepository`（`workflow/memory_context.py:73-116`），校验 content hash、用户确认冲突，并重新执行 `validate_metadata`/`finalize_and_validate_metrics`（`117-195`）。它兼具 application policy 与 MySQL adapter 调用；领域校验函数位于 `ddl_metadata/validation.py`，但 loader 直接依赖 `MySQLDatabase`，边界不完全纯。
- `persist_node` 在写入前恢复所有强类型 checkpoint JSON（`workflow/nodes.py:472-500`），调用 snapshot port `dependencies.snapshot.persist`，事务成功后才返回 SUCCEEDED（`496-515`）。

## MySQL snapshot 与索引数据流

- `MetadataSnapshotService.persist` 计算 accepted memory/fingerprints、解析命名 source→database（`snapshots.py:76-110`），获取 generation advisory locks 后开启 `MySQLDatabase.session`（`117-128`）。
- 同一事务依次：读取旧 Meta scope/待过期 memory keys（`128-136`）；过期不兼容权威 memory、同步 Meta（`137-145`）；写 DataSync desired state（`155-157`）；写 semantic index desired/outbox（`159-197`）；最后 upsert accepted memory/audit/relations（`198-200`）。异常回滚；lock contention 转 `DataAgentError(generation_lock_unavailable, retryable=True)`（`200-207`）。
- 角色：`MetadataSnapshotService` 是 application persistence facade；`MetadataRepository`/`MemoryRepository`/`DataSyncRepository` 是 MySQL driven adapters；`MySQLDatabase` 是 infrastructure。单事务是关键一致性 seam，唯一成功事实出口为 `persist_snapshot`。

## Redis/SSE 响应流

- GET 状态读取 `DDLJobStore.get` 的公开投影（`api/jobs.py:58-64`）。SSE 先校验存在、读取 stream tail、再次读取 Job Hash（`api/jobs.py:67-86`），避免首帧与游标竞态。
- `stream_job_events` 首发权威 snapshot；随后 Redis Stream `read_events` 阻塞读取，游标仅随已发送事件前进；超时重读 Job Hash，若通知丢失则补发 snapshot，否则发送 heartbeat（`api/job_events.py:39-91`）。Redis 异常只输出固定 `stream_unavailable` 事件（`92-117`）。终态 `SUCCEEDED/REJECTED/FAILED` 结束流（`23-27`, `128-132`）。
- 角色：SSE generator 是输出 adapter；Redis Stream 是通知日志而非权威状态，Job Hash 是 source of truth；snapshot repair 是可靠性 seam。

## 分层匹配结论（事实与存疑分开）

事实：HTTP 路由、Redis/arq、LangGraph、MySQL/Redis checkpoint 均有明确 adapter 边界；应用启动集中装配依赖；graph 通过 contracts 注入 model/memory/snapshot；MySQL snapshot 单事务和 Redis CAS/outbox 提供一致性边界。

存疑/不完全匹配：`workflow/memory_context.py` 的 `MemoryContextLoader` 同时承载用例策略、领域重校验和直接 `MySQLDatabase` 访问（`11-15`, `90-110`），使 application/domain 与 infrastructure 耦合；`MetadataSnapshotService` 直接构造具体 repositories（`123-136`, `156`, `195`），其 port 仅在 graph contracts 暴露，严格 Ports-and-Adapters 仍是渐进式而非完全隔离。领域实体/规则主要在 `models.*` 与 `validation.py`，未见独立 domain 聚合层承接整个请求。
