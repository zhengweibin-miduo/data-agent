"""Query generation 共享读协调适配器测试。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import Mock

import pytest
from langchain_core.tools import BaseTool

from errors import DataAgentError
from infrastructure.mysql import (
    AdvisoryLockReleaseError,
    AdvisoryLockUnavailableError,
)
from query.adapters import readiness as readiness_module
from query.adapters.readiness import QueryReadinessAdapter


async def test_query_hold_uses_executor_as_the_only_generation_owner() -> None:
    """Query 必须只在执行连接上一次持有全部 generation target。"""
    executor_holds: list[tuple[tuple[str, ...], int]] = []

    @asynccontextmanager
    async def execution_locks(
        names: tuple[str, ...], timeout_seconds: int
    ) -> AsyncIterator[None]:
        executor_holds.append((names, timeout_seconds))
        yield

    executor = Mock()
    executor.hold_generation = execution_locks
    adapter = QueryReadinessAdapter(
        cast(BaseTool, object()),
        executor,
        dw_database="dw",
        lock_timeout=2,
    )

    async with adapter.hold(("z_table", "a_table")):
        pass

    assert executor_holds == [
        (
            (
                "dsg:z_table:BlojryERPAgRWSVK_OKCvpGlg7eXeRFHmmJjHUs83rQ",
                "dsg:a_table:xhq1axZg_N1RqNZfub6vVqXyuQhRbeY-7BJ3f_eFwh0",
            ),
            2,
        )
    ]


async def test_query_hold_maps_lock_contention_to_retryable_conflict() -> None:
    """共享锁竞争必须成为首事件前可安全投影的 409 业务错误。"""

    @asynccontextmanager
    async def unavailable(
        names: tuple[str, ...] | list[str],
        timeout_seconds: int,
    ) -> AsyncIterator[None]:
        del names, timeout_seconds
        raise AdvisoryLockUnavailableError("busy")
        yield

    executor = Mock()
    executor.hold_generation = unavailable
    adapter = QueryReadinessAdapter(
        cast(BaseTool, object()),
        executor,
        dw_database="dw",
        lock_timeout=1,
    )

    with pytest.raises(DataAgentError) as captured:
        async with adapter.hold(("orders",)):
            pytest.fail("锁竞争时不得越过 Query readiness 协调门禁")

    assert captured.value.code == "generation_lock_unavailable"
    assert captured.value.stage == "query_readiness"
    assert captured.value.retryable is True
    assert captured.value.http_status == 409


async def test_query_hold_logs_release_failure_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已完成 Query 的 owner 连接失效不得追加终态后的 stream_error。"""

    @asynccontextmanager
    async def release_fails(
        names: tuple[str, ...] | list[str],
        timeout_seconds: int,
    ) -> AsyncIterator[None]:
        del names, timeout_seconds
        yield
        raise AdvisoryLockReleaseError("owner connection invalidated")

    warning = Mock()
    executor = Mock()
    executor.hold_generation = release_fails
    monkeypatch.setattr(readiness_module.logger, "warning", warning)
    adapter = QueryReadinessAdapter(
        cast(BaseTool, object()),
        executor,
        dw_database="dw",
        lock_timeout=1,
    )

    async with adapter.hold(("orders",)):
        pass

    warning.assert_called_once()
