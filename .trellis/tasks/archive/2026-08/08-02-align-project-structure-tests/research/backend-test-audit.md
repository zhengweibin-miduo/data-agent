# 后端测试臃肿审计（只读取证）

## 规模统计

基于 `tests/**/*.py` 静态扫描（88 个 Python 文件，合计约 19,758 行）：

- 统计到 373 个 `test_*` 函数；`@pytest.fixture` 仅 2 个（无 `conftest.py`），说明大量 setup 直接写在测试模块中。
- 最大文件：`tests/unit/metadata_indexing/test_runtime.py` 1,189 行/31 tests；`test_value_refresh.py` 884 行/20 tests；`test_outbox.py` 800 行/17 tests；集成 `tests/integration/persistence/test_memory_repository.py` 784 行/6 tests。
- 其他大文件：`tests/unit/data_sync/test_schema_sync.py` 617 行/24 tests，`tests/unit/memory/test_index_dispatcher.py` 571 行/10 tests。

## 明确违反/高风险耦合候选

1. `tests/unit/data_sync/test_service.py:28-66,104-138,141-173,209-239` 反复构造 `AsyncMock` repository/source，并 monkeypatch `DataSyncRepository`、`MySQLDatabase.session`；同时直接替换和断言私有 `_process`、`_capture`、`_retry`、`_reschedule`、`_with_lease_heartbeat`（例如 54-66、108-116、214-223）。`assert_awaited_once_with`/`await_count` 断言内部 collaborator 调用。按质量规范，应优先改为 DataSyncService 公共 driving seam 的行为断言；可删除仅验证调用次数/顺序的断言，保留阶段状态/持久化结果。

2. `tests/unit/metadata_indexing/test_runtime.py` 导入并直接测试大量私有符号（`_async_bulk_chunks`、`_pending_value_scope_statement`、`_finalize_value_results`、`_refresh_generation_matches`、`_semantic_candidate_limit`、`_value_candidate_limit` 等，行 28-56）；前 140 行已有多个“编译 SQL/配置常量/候选上限”单元断言，后续 1,189 行继续混合 dispatcher、Elasticsearch、Qdrant、恢复事务场景。建议按公共边界拆为 search/projection/dispatcher/index-adapter 三组；私有 helper 的纯实现断言可合并为少量公共行为回归。

3. `tests/unit/metadata_indexing/test_runtime.py`、`test_value_refresh.py`、`test_outbox.py` 三个 800+ 行文件均覆盖 refresh generation、pending/current 版本、cursor/budget、stale generation/cleanup 等同一 metadata-indexing 生命周期。可按 seam（repository 状态迁移、refresh scan、search publication）拆分并去重；仅保留每个规则一个代表性正常例和一个边界/错误例。当前测试名证据：`test_runtime.py:193-214,280-332,601-752`；`test_value_refresh.py:97-222,293-399,545-724,778-830`；`test_outbox.py:333-425,518-676`。

4. `tests/integration/persistence/test_memory_repository.py` 784 行仅 6 tests，疑似每个测试拥有超宽数据库 fixture/setup；应检查是否可提取共享 integration fixture（UUID source/stable IDs、scoped cleanup），但需在重构前逐段确认。

## 可复用 fake/factory 现状

`tests/helpers/fakes.py:42-232` 已有 `_NoMemory`、`_CompleteMemory`、`_Snapshot`、`FakeMetadataGenerator`，后者通过 flags 注入多种错误并维护 `classify_calls/question_calls/metric_calls`。质量规范要求可复用 fake/factory 放在测试模块外；新增 fake 前应复用此处。相反，`test_service.py` 等模块内反复 `AsyncMock()` setup，尚未抽成共享 factory。

## call count/order 与 collaborator mock 统计线索

全仓 grep 到 `assert_awaited_once_with`、`assert_called_once`、`call_count` 等 41 个匹配，主要集中在 `tests/unit/data_sync/test_service.py` 以及 integration infrastructure。`test_service.py:167-173` 还通过 `apply_event.await_args_list` 检查逐事件调用顺序/数量；这属于 implementation-coupled 断言，除非顺序是明确外部契约，否则应删除或改为最终阶段/数据结果断言。

## 建议优先级（候选，不是已决定方案）

- P0：整理 `test_service.py` 的私有方法 mock 与 call-count/order 断言，围绕 `dispatch_once`/公开同步流程和可观测 task phase、repository state 建立最小 seam 测试。
- P1：将 `metadata_indexing` 三个超大模块按 projection/search/worker-adapter seam 拆分，合并重复 refresh-generation/cursor 场景。
- P1：把重复的 repository/source/settings fake setup 提取至 `tests/helpers/fakes.py` 或专用 fixture 模块；避免在 `test_*.py` 内继续定义可复用 fake。
- P2：审查 `integration/persistence/test_memory_repository.py` 的宽 fixture，确认是否可共享 fixture 与分层（repository contract vs transaction/error）而不降低真实 MySQL 边界覆盖。

以上均为静态证据驱动候选；未执行删除、合并或业务代码修改，也未断言某个测试必然冗余，需结合对应公共 interface 复核。
