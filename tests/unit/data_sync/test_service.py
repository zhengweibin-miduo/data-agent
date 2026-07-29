"""数据同步服务阶段门禁检查。"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from tests.helpers.checks import check_condition, check_equal
from tests.unit.data_sync.test_repository import _streaming_task

from data_agent.data_sync import service
from data_agent.data_sync.binlog import BinlogCaptureResult
from data_agent.data_sync.locks import generation_lock_name
from data_agent.data_sync.models import (
    BinlogCoordinate,
    RowOperation,
    SyncPhase,
    SyncRowEvent,
)
from data_agent.data_sync.schema_sync import SchemaLockUnavailableError
from data_agent.data_sync.service import DataSyncService
from data_agent.infrastructure.mysql import AdvisoryLockUnavailableError


async def test_dispatch_claims_only_the_task_it_can_start_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """串行 dispatcher 不得提前领取尚未开始续租的后续任务。"""
    repository = AsyncMock()
    repository.claim_tasks.return_value = []
    monkeypatch.setattr(service, "DataSyncRepository", lambda session: repository)
    monkeypatch.setattr(service.MySQLDatabase, "session", _fake_session)
    sync_service = DataSyncService({"a": AsyncMock(), "b": AsyncMock()}, AsyncMock())

    await sync_service.dispatch_once()

    assert repository.claim_tasks.await_args.kwargs["limit"] == 1


async def test_streaming_backlog_returns_to_replaying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实时捕获留下待应用事件时立即关闭回答就绪门禁。"""
    task = _streaming_task()
    repository = AsyncMock()
    repository.read_events.return_value = [object()]
    monkeypatch.setattr(service, "DataSyncRepository", lambda session: repository)
    monkeypatch.setattr(service.MySQLDatabase, "session", _fake_session)
    monkeypatch.setattr(service, "apply_buffered_event", AsyncMock())
    source = AsyncMock()
    sync_service = DataSyncService({"local": source}, AsyncMock())
    sync_service._capture = AsyncMock()

    async def finish_operation(task: object, work: Awaitable[Any]) -> object:
        return await work

    sync_service._with_lease_heartbeat = AsyncMock(side_effect=finish_operation)
    sync_service._settings = AsyncMock(event_cleanup_batch_size=100)

    await sync_service._process(task)

    sync_service._capture.assert_not_awaited()
    repository.settle_phase.assert_awaited_once_with(task, SyncPhase.REPLAYING)


async def test_capture_atomically_marks_streaming_task_replaying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新事件及位点提交时必须在同一事务中撤销实时就绪状态。"""
    coordinate = BinlogCoordinate(file="mysql-bin.000001", position=120, row_index=0)
    task = replace(_streaming_task(), captured=coordinate)
    repository = AsyncMock()
    repository.count_pending_events.return_value = 0
    monkeypatch.setattr(service, "DataSyncRepository", lambda session: repository)
    monkeypatch.setattr(service.MySQLDatabase, "session", _fake_session)
    event = SyncRowEvent(
        source="local",
        source_schema=task.desired.source_schema,
        source_table=task.desired.source_table,
        coordinate=coordinate,
        operation=RowOperation.INSERT,
        after={"id": 1},
    )
    source = AsyncMock()
    source.capture.return_value = BinlogCaptureResult(events=(event,), tail=coordinate)
    sync_service = DataSyncService(
        {"local": source},
        AsyncMock(event_buffer_limit=100),
    )

    await sync_service._capture(task, source)

    repository.append_events.assert_awaited_once_with(task.id, (event,))
    repository.advance_captured_coordinate.assert_awaited_once_with(
        task,
        coordinate,
        has_new_events=True,
    )


async def test_unexpected_error_persists_phase_and_exception_type() -> None:
    """未分类异常应留下安全且可定位的阶段与异常类型。"""
    task = _streaming_task()
    sync_service = DataSyncService({"local": AsyncMock()}, AsyncMock())
    sync_service._process = AsyncMock(side_effect=ValueError("sensitive row data"))
    sync_service._retry = AsyncMock()

    await sync_service._process_safely(task)

    sync_service._retry.assert_awaited_once_with(
        task,
        "unexpected_sync_error:streaming:ValueError",
    )


async def test_unexpected_error_logs_redacted_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未分类异常日志保留堆栈，但不得包含原始敏感消息。"""
    task = _streaming_task()
    sync_service = DataSyncService({"local": AsyncMock()}, AsyncMock())
    sync_service._process = AsyncMock(side_effect=ValueError("sensitive row data"))
    sync_service._retry = AsyncMock()
    safe_logger = Mock()
    logger = Mock()
    logger.opt.return_value = safe_logger
    monkeypatch.setattr(service, "logger", logger)

    await sync_service._process_safely(task)

    exception_info = logger.opt.call_args.kwargs["exception"]
    assert exception_info[0] is RuntimeError
    assert "ValueError" in str(exception_info[1])
    assert "sensitive row data" not in str(exception_info[1])
    safe_logger.error.assert_called_once()


async def test_backfill_drains_event_and_keeps_progress_when_buffer_is_saturated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """捕获缓冲饱和后腾挪事件并继续推进既有回填游标。"""
    task = replace(_streaming_task(), phase=SyncPhase.BACKFILLING)
    repository = AsyncMock()
    monkeypatch.setattr(service, "DataSyncRepository", lambda session: repository)
    monkeypatch.setattr(service.MySQLDatabase, "session", _fake_session)
    read_batch = AsyncMock(return_value=[])
    monkeypatch.setattr(service, "read_backfill_batch", read_batch)
    apply_event = AsyncMock()
    monkeypatch.setattr(service, "apply_buffered_event", apply_event)
    events = [object(), object(), object()]
    repository.read_events.return_value = events
    settings = AsyncMock(event_cleanup_batch_size=3)
    sync_service = DataSyncService({"local": AsyncMock()}, settings)
    sync_service._capture = AsyncMock(return_value=False)
    sync_service._renew_lease = AsyncMock()

    async def finish_operation(task: object, work: Awaitable[Any]) -> object:
        return await work

    sync_service._with_lease_heartbeat = AsyncMock(side_effect=finish_operation)

    await sync_service._process(task)

    repository.read_events.assert_awaited_once_with(task.id, limit=3)
    assert [call.args[2] for call in apply_event.await_args_list] == events
    assert sync_service._renew_lease.await_count == len(events)
    repository.cleanup_events.assert_awaited_once_with(task.id, limit=3)
    read_batch.assert_awaited_once()
    repository.restart_backfill.assert_not_awaited()
    repository.settle_phase.assert_awaited_once_with(task, SyncPhase.REPLAYING)


async def test_schema_lock_contention_does_not_consume_retry_budget() -> None:
    """正常结构锁竞争只重新调度，不进入失败退避。"""
    task = replace(_streaming_task(), phase=SyncPhase.PENDING_SCHEMA)
    settings = AsyncMock(retry_base_seconds=5)
    sync_service = DataSyncService({"local": AsyncMock()}, settings)
    sync_service._process = AsyncMock(
        side_effect=SchemaLockUnavailableError("schema lock busy")
    )
    sync_service._reschedule = AsyncMock()
    sync_service._retry = AsyncMock()

    await sync_service._process_safely(task)

    sync_service._reschedule.assert_awaited_once_with(task)
    sync_service._retry.assert_not_awaited()


async def test_generation_lock_contention_does_not_consume_retry_budget() -> None:
    """Generation 锁竞争只重新调度，不得增加任务失败次数。"""
    task = replace(_streaming_task(), phase=SyncPhase.PENDING_SCHEMA)
    sync_service = DataSyncService({"local": AsyncMock()}, AsyncMock())
    sync_service._process = AsyncMock(
        side_effect=AdvisoryLockUnavailableError("generation lock busy")
    )
    sync_service._reschedule = AsyncMock()
    sync_service._retry = AsyncMock()

    await sync_service._process_safely(task)

    check_equal("generation 竞争重新调度次数", sync_service._reschedule.await_count, 1)
    check_equal("generation 竞争失败退避次数", sync_service._retry.await_count, 0)


async def test_schema_sync_holds_generation_lock_through_session_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一 DDL Session 必须承担 authority 回调和 phase settlement。"""
    task = replace(_streaming_task(), phase=SyncPhase.PENDING_SCHEMA)
    events: list[str] = []
    ddl_session = AsyncMock()
    repository = AsyncMock()
    repository.has_authority.return_value = True
    repository.settle_phase.return_value = True

    @asynccontextmanager
    async def generation_locks(
        names: Iterable[str],
        *,
        timeout_seconds: float,
    ) -> AsyncIterator[None]:
        check_equal(
            "worker 使用目标共享 generation 锁",
            list(names),
            [generation_lock_name("dw", task.desired.target_table)],
        )
        check_equal("worker 使用配置的锁等待预算", timeout_seconds, 2)
        events.append("generation_enter")
        yield
        events.append("generation_exit")

    @asynccontextmanager
    async def ddl_session_context() -> AsyncIterator[AsyncMock]:
        events.append("session_enter")
        yield ddl_session
        events.append("session_commit")

    class FakeSynchronizer:
        """记录 schema 层回调所用 repository authority。"""

        def __init__(self, session: object, *, database: str) -> None:
            check_condition(
                "schema synchronizer 复用 DDL Session",
                session is ddl_session,
            )

        async def synchronize(
            self,
            desired: object,
            *,
            check_authority: Callable[[], Awaitable[bool]] | None = None,
        ) -> None:
            events.append("schema_enter")
            if check_authority is not None:
                check_condition("DDL Session authority 有效", await check_authority())
            events.append("schema_exit")

    async def finish_operation(task_value: object, operation: Awaitable[Any]) -> Any:
        return await operation

    monkeypatch.setattr(service.MySQLDatabase, "advisory_locks", generation_locks)
    monkeypatch.setattr(service.MySQLDatabase, "session", ddl_session_context)
    monkeypatch.setattr(service, "DataSyncRepository", lambda session: repository)
    monkeypatch.setattr(service, "DWSchemaSynchronizer", FakeSynchronizer)
    sync_service = DataSyncService(
        {"local": AsyncMock()},
        AsyncMock(
            dw_database="dw",
            generation_lock_timeout_seconds=2,
        ),
    )
    sync_service._with_lease_heartbeat = AsyncMock(side_effect=finish_operation)

    await sync_service._synchronize_schema(task)

    check_condition(
        "authority 与 settlement 使用同一 repository",
        repository.has_authority.await_count == 1
        and repository.settle_phase.await_count == 1,
    )
    check_equal(
        "generation 锁跨越 DDL Session 提交",
        events,
        [
            "generation_enter",
            "session_enter",
            "schema_enter",
            "schema_exit",
            "session_commit",
            "generation_exit",
        ],
    )


async def test_lease_heartbeat_waits_for_cancelled_operation_cleanup() -> None:
    """取消长步骤时必须等待其 finally 清理完毕后再退出 Session 上层。"""
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def operation() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    sync_service = DataSyncService(
        {"local": AsyncMock()},
        AsyncMock(claim_lease_seconds=60),
    )
    running = asyncio.create_task(
        sync_service._with_lease_heartbeat(_streaming_task(), operation())
    )
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    check_condition("取消返回前已完成长步骤清理", cleaned.is_set())


class _fake_session:
    """提供服务单元测试所需的异步 Session 上下文。"""

    async def __aenter__(self) -> AsyncMock:
        return AsyncMock()

    async def __aexit__(self, *args: object) -> None:
        return None
