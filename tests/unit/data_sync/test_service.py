"""数据同步服务阶段门禁检查。"""

from collections.abc import Awaitable
from typing import Any
from unittest.mock import AsyncMock

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

    repository.settle_phase.assert_awaited_once_with(task, SyncPhase.REPLAYING)


class _fake_session:
    """提供服务单元测试所需的异步 Session 上下文。"""

    async def __aenter__(self) -> AsyncMock:
        return AsyncMock()

    async def __aexit__(self, *args: object) -> None:
        return None
