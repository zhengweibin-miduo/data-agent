"""数据同步服务阶段门禁检查。"""

from collections.abc import Awaitable
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from tests.unit.data_sync.test_repository import _streaming_task

from data_agent.data_sync import service
from data_agent.data_sync.binlog import BinlogCaptureResult
from data_agent.data_sync.models import (
    BinlogCoordinate,
    RowOperation,
    SyncPhase,
    SyncRowEvent,
)
from data_agent.data_sync.schema_sync import SchemaLockUnavailableError
from data_agent.data_sync.service import DataSyncService


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

    repository.append_event.assert_awaited_once_with(task.id, event)
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
    repository.read_events.return_value = [object()]
    sync_service = DataSyncService({"local": AsyncMock()}, AsyncMock())
    sync_service._capture = AsyncMock(return_value=False)

    async def finish_operation(task: object, work: Awaitable[Any]) -> object:
        return await work

    sync_service._with_lease_heartbeat = AsyncMock(side_effect=finish_operation)

    await sync_service._process(task)

    apply_event.assert_awaited_once()
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


class _fake_session:
    """提供服务单元测试所需的异步 Session 上下文。"""

    async def __aenter__(self) -> AsyncMock:
        return AsyncMock()

    async def __aexit__(self, *args: object) -> None:
        return None
