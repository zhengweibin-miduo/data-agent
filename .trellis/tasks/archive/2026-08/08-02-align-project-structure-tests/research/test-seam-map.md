# 架构重构测试 seam 映射

本文件结合 `tdd` 与 `codebase-design`：调用者和测试应穿过同一个 module interface。以下六类 seam 已获用户确认，是后续新增/保留测试的边界；实现开始前仍需通过 Trellis 规划与激活门禁。

## Seam 1：DDL Job HTTP / lifecycle

- **Interface**：提交、读取、回答澄清、SSE 观察终态；公开状态、幂等、revision、retryable error 是契约。
- **现有低成本证据**：`tests/integration/test_api.py`、`test_ddl_metadata_flow.py`、`test_job_events.py`，以及 `tests/unit/ddl_metadata/jobs/redis/test_job_stores.py`。
- **保留**：HTTP/SSE 可观察行为、Redis CAS/outbox/reconnect、worker 从 accepted job 到 terminal state 的场景。
- **替换候选**：`worker/test_retryable_contract.py:9`、`worker/test_job_runner.py:11` 直接导入私有 `_is_retryable`、`_task_start_stage`；当错误分类和 stage projection 进入稳定 interface 后，以 lifecycle 结果替换私有函数测试。

## Seam 2：Accepted Meta Snapshot 发布

- **Interface**：一次 accepted snapshot 要么原子提交 Meta Snapshot、Long-term Memory、Data Sync desired state 和索引 outbox，要么全部回滚；generation lock 覆盖事务提交。
- **现有覆盖**：`tests/unit/ddl_metadata/test_snapshots.py:34-140` 安装四类 repository fake，`147-367` 通过 monkeypatch 验证锁、提交、回滚和错误映射。
- **问题**：行为重要，但 fake 通过替换具体 implementation 装入；测试暴露了 `MetadataSnapshotService` 没有显式 publication ports。
- **重构方向**：保留原子性/锁顺序/失败回滚四个行为，将具体 repositories 收拢为 publication adapter 或显式 ports；测试通过 `persist` interface 注入 fake adapters，不断言内部 repository 调用序列之外的实现细节。

## Seam 3：Conversation 与 Long-term Memory

- **Interface**：`ConversationService` 的创建、轮次门禁、完成、上下文与用户数据删除；`MemoryService` 的 search/get/history/update/delete；MySQL 是权威，ES/Qdrant 是可重建 projection。
- **现有覆盖**：`tests/integration/persistence/test_conversation_repository.py`、`test_memory_repository.py`、`tests/integration/test_memory_services.py`，以及 unit memory domain/search/dispatcher tests。
- **问题**：`ConversationService` 直接使用 `MySQLDatabase`、`ConversationRepository`、`MemoryRepository`，使跨 context port 不可替换；`conversation/test_conversation.py:9` 直接测试私有 `_validated_candidates`。
- **替换策略**：保留 turn/outbox/idempotency/tenant isolation/tombstone-before-purge 等行为；把 Conversation→Memory 协作放在 application port，页面/HTTP 与内部调用者通过同一 interface 测试。私有 candidate validation 仅在它被提炼为独立纯 domain policy interface 后保留，否则用 extraction outcome 覆盖。

## Seam 4：Data Sync task lifecycle

- **Interface**：`dispatch_once` 驱动一个有界任务步骤；可观察结果是 task phase、captured/applied coordinate、DW rows、lease/error/backoff，而不是调用了哪个私有方法。
- **现有问题证据**：`tests/unit/data_sync/test_service.py:54-66,94-116,157-173,194-239,321-364` 直接调用或替换 `_process`、`_capture`、`_retry`、`_reschedule`、`_with_lease_heartbeat`，并断言 collaborator call count/order。
- **保留**：`tests/integration/data_sync/test_cdc_pipeline.py` 的真实 CDC convergence；schema lock、lease loss、retry classification 等外部可见状态。
- **替换策略**：为 task repository、source reader、DW writer、clock/lease 定义 application ports，测试统一从 `dispatch_once` 进入，用 in-memory adapters 观察 task/DW 结果；新 interface 覆盖后删除浅层私有方法测试，而不是叠加一套新测试。

## Seam 5：Metadata Projection lifecycle / search

- **Interface**：accepted desired state → outbox claim → semantic/value projection 收敛；搜索只返回经 Meta authoritative read 复核的结果。
- **现有问题证据**：`tests/unit/metadata_indexing/test_runtime.py` 1,189 行并导入多类私有 helper；`:775,838` 直接调用 `MetadataIndexDispatcher._synchronize`。`test_value_refresh.py`、`test_outbox.py` 与它重复覆盖 generation/cursor/phase/recovery。
- **替换策略**：确认 `metadata_indexing` 的 bounded context 归属后，保留 `dispatch` 与 `MetadataSearchService.search_metadata` 两个外部 interface；将纯 desired/version policy 作为 domain interface 单测，将 MySQL/ES/Qdrant 作为 adapters。按“正常收敛、迟到 generation、重试恢复、删除/重建”各保留代表性 tracer cases，删除已被深 module interface 覆盖的私有 helper 测试。

## Seam 6：Frontend transport 与 feature orchestration

- **Interface**：`apiRequest`/endpoint adapters 负责 HTTP 验证与稳定错误；`jobEvents` 负责 SSE/reconnect/polling；Workbench/Knowledge 页面负责用户可观察状态。
- **现有问题证据**：`WorkbenchPage.test.tsx:11-22` mock 内部 API modules，`:79-82,137-140` 直接调用 `connectJobEvents.mock.calls[0][1]` 暴露的内部回调；706 行文件还重复 preview→submit setup。SSE adapter 已在 `jobEvents.test.ts` 覆盖部分相同机制。
- **替换策略**：adapter tests 完整证明 payload validation、reconnect、polling、authoritative GET；feature tests 只证明恢复、提交、澄清、chat 的可观察 UI 行为。用窄 factory/helper 消除 setup，不能创建覆盖所有 feature 的全局 fixture。

## 不应机械删除的断言

- 外部调用次数本身是业务预算时可以保留，例如 LLM 只允许一次 repair、事件重试上限、幂等请求不重复发送。
- 事务提交/回滚、租约续期、事件顺序若是公开一致性契约，可以在 adapter contract test 中验证；不能仅因使用 `call_count` 就删除。
- 集成测试直接查询数据库可作为 repository/transaction seam 的证据；若被测试 interface 是 HTTP/UI，则不应绕过 interface 用数据库侧信道证明。

## 完成验证门禁（规划）

只有在实现后执行并读取完整输出，才能声称完成：

1. `uv lock --check`
2. `uv run ruff check src tests`
3. `uv run pyright src tests`
4. `uv run python -m compileall -q src tests`
5. `uv run python -m data_agent.settings`
6. `uv run pytest -m "not integration"`
7. 相关 MySQL/Redis/CDC/索引集成模块；服务不可用时必须如实记录。
8. `npm ci && npm run lint && npm run typecheck && npm run test && npm run build`（从 `frontend/`）
9. `git diff --check`

回归测试必须完成真实 red/green 证明：新增测试通过后，临时撤去对应修复时必须失败，再恢复修复并重新通过。
