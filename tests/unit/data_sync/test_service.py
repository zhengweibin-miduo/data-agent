"""数据同步服务阶段门禁检查。"""

from collections.abc import Awaitable
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from tests.unit.data_sync.test_repository import _streaming_task

from data_agent.data_sync import service
from data_agent.data_sync.models import SyncPhase
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


async def test_backfill_stops_when_event_buffer_is_saturated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """捕获缓冲饱和后不得推进未受 CDC 保护的历史游标。"""
    task = replace(_streaming_task(), phase=SyncPhase.BACKFILLING)
    repository = AsyncMock()
    monkeypatch.setattr(service, "DataSyncRepository", lambda session: repository)
    monkeypatch.setattr(service.MySQLDatabase, "session", _fake_session)
    read_batch = AsyncMock()
    monkeypatch.setattr(service, "read_backfill_batch", read_batch)
    sync_service = DataSyncService({"local": AsyncMock()}, AsyncMock())
    sync_service._capture = AsyncMock(return_value=False)

    async def finish_operation(task: object, work: Awaitable[Any]) -> object:
        return await work

    sync_service._with_lease_heartbeat = AsyncMock(side_effect=finish_operation)

    await sync_service._process(task)

    read_batch.assert_not_awaited()
    repository.settle_phase.assert_awaited_once_with(task, SyncPhase.BACKFILLING)


class _fake_session:
    """提供服务单元测试所需的异步 Session 上下文。"""

    async def __aenter__(self) -> AsyncMock:
        return AsyncMock()

    async def __aexit__(self, *args: object) -> None:
        return None
