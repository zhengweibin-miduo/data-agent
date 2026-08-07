"""Data Sync MySQL 与 lease adapters 合约检查。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from tests.helpers.data_sync import sync_task

from data_sync.adapters import mysql as mysql_adapters
from data_sync.application.contracts import CapturedEvents
from data_sync.locks import generation_lock_name
from data_sync.models import (
    BinlogCoordinate,
    RowOperation,
    SyncPhase,
    SyncRowEvent,
)
from settings import app_config


async def test_materialization_holds_generation_lock_through_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generation 锁跨越 DDL authority、settlement 和 Session commit。"""
    task = sync_task(SyncPhase.PENDING_SCHEMA)
    events: list[str] = []
    ddl_session = AsyncMock()
    provenance_session = AsyncMock()
    repository = AsyncMock()
    repository.has_authority.return_value = True
    repository.settle_phase.return_value = True

    @asynccontextmanager
    async def generation_locks(
        names: Iterable[str],
        *,
        timeout_seconds: int,
    ) -> AsyncIterator[None]:
        assert list(names) == [generation_lock_name("dw", task.desired.target_table)]
        assert timeout_seconds == 2
        events.append("generation_enter")
        yield
        events.append("generation_exit")

    sessions = iter((ddl_session, provenance_session))

    @asynccontextmanager
    async def session_context() -> AsyncIterator[AsyncMock]:
        selected = next(sessions)
        events.append("ddl_enter" if selected is ddl_session else "snapshot_enter")
        yield selected
        events.append("ddl_commit" if selected is ddl_session else "snapshot_commit")

    class FakeSynchronizer:
        """记录 schema adapter 的 authority 与 provenance Session。"""

        def __init__(self, session: object, *, database: str) -> None:
            assert session is ddl_session
            assert database == "dw"

        def with_provenance_session(self, session: object) -> FakeSynchronizer:
            """确认独立一致性快照。"""
            assert session is provenance_session
            return self

        async def synchronize(
            self,
            desired: object,
            *,
            check_authority: Callable[[], Awaitable[bool]] | None = None,
        ) -> None:
            """在 schema 临界区检查同一 repository authority。"""
            events.append("schema_enter")
            assert check_authority is not None
            assert await check_authority()
            events.append("schema_exit")

    monkeypatch.setattr(
        mysql_adapters.MySQLDatabase,
        "exclusive_service_locks",
        generation_locks,
    )
    monkeypatch.setattr(mysql_adapters.MySQLDatabase, "session", session_context)
    monkeypatch.setattr(
        mysql_adapters,
        "DataSyncRepository",
        lambda session: repository,
    )
    monkeypatch.setattr(mysql_adapters, "DWSchemaSynchronizer", FakeSynchronizer)
    settings = app_config.data_sync.model_copy(
        update={
            "dw_database": "dw",
            "generation_lock_timeout_seconds": 2,
        }
    )
    adapter = mysql_adapters.MySQLMaterializationAdapter(
        settings,
        lambda session, desired: AsyncMock(),
    )

    await adapter.synchronize_schema(task)

    assert repository.has_authority.await_count == 1
    repository.settle_phase.assert_awaited_once_with(task, SyncPhase.BUFFERING)
    assert events == [
        "generation_enter",
        "ddl_enter",
        "snapshot_enter",
        "schema_enter",
        "schema_exit",
        "snapshot_commit",
        "ddl_commit",
        "generation_exit",
    ]


async def test_generation_reset_holds_write_lock_through_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generation reset 的 DW 与 task 事务提交后才释放 WRITE lock。"""
    task = sync_task(SyncPhase.BUFFERING)
    coordinate = BinlogCoordinate(file="mysql-bin.000001", position=4, row_index=0)
    events: list[str] = []
    session = AsyncMock()
    repository = AsyncMock()
    repository.has_authority.return_value = True
    repository.record_snapshot.return_value = True
    repository.advance_captured_coordinate.return_value = True
    repository.settle_phase.return_value = True

    @asynccontextmanager
    async def generation_locks(
        names: Iterable[str],
        *,
        timeout_seconds: int,
    ) -> AsyncIterator[None]:
        assert list(names) == [generation_lock_name("dw", task.desired.target_table)]
        assert timeout_seconds == 2
        events.append("generation_enter")
        yield
        events.append("generation_exit")

    @asynccontextmanager
    async def session_context() -> AsyncIterator[AsyncMock]:
        events.append("transaction_enter")
        yield session
        events.append("transaction_commit")

    async def reset_rows(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        events.append("reset_rows")
        return True

    monkeypatch.setattr(
        mysql_adapters.MySQLDatabase,
        "exclusive_service_locks",
        generation_locks,
    )
    monkeypatch.setattr(mysql_adapters.MySQLDatabase, "session", session_context)
    monkeypatch.setattr(mysql_adapters, "DataSyncRepository", lambda _: repository)
    monkeypatch.setattr(mysql_adapters, "reset_source_rows", reset_rows)
    settings = app_config.data_sync.model_copy(
        update={
            "dw_database": "dw",
            "generation_lock_timeout_seconds": 2,
        }
    )
    adapter = mysql_adapters.MySQLMaterializationAdapter(
        settings,
        lambda current_session, desired: AsyncMock(),
    )

    await adapter.reset_generation(task, coordinate, limit=100)

    assert events == [
        "generation_enter",
        "transaction_enter",
        "reset_rows",
        "transaction_commit",
        "generation_exit",
    ]
    repository.record_snapshot.assert_awaited_once_with(task, coordinate)
    repository.advance_captured_coordinate.assert_awaited_once_with(task, coordinate)
    repository.settle_phase.assert_awaited_once_with(task, SyncPhase.BACKFILLING)


async def test_task_adapter_records_capture_and_coordinate_in_one_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事件追加与 captured coordinate/readiness 变化复用同一 repository。"""
    start = BinlogCoordinate(file="mysql-bin.000001", position=100, row_index=0)
    tail = BinlogCoordinate(file="mysql-bin.000001", position=120, row_index=0)
    task = sync_task(SyncPhase.STREAMING, captured=start)
    event = SyncRowEvent(
        source=task.desired.source,
        source_schema=task.desired.source_schema,
        source_table=task.desired.source_table,
        coordinate=tail,
        operation=RowOperation.INSERT,
        after={"id": 1},
    )
    repository = AsyncMock()
    repository.advance_captured_coordinate.return_value = True

    @asynccontextmanager
    async def session_context() -> AsyncIterator[AsyncMock]:
        yield AsyncMock()

    monkeypatch.setattr(mysql_adapters.MySQLDatabase, "session", session_context)
    monkeypatch.setattr(
        mysql_adapters,
        "DataSyncRepository",
        lambda session: repository,
    )

    await mysql_adapters.MySQLSyncTaskAdapter().record_capture(
        task,
        CapturedEvents(events=(event,), tail=tail),
    )

    repository.append_events.assert_awaited_once_with(task.id, (event,))
    repository.advance_captured_coordinate.assert_awaited_once_with(
        task,
        tail,
        has_new_events=True,
    )


async def test_lease_coordinator_waits_for_cancelled_operation_cleanup() -> None:
    """取消长步骤时 adapter 在返回前等待 operation finally 完成。"""
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def operation() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    tasks = AsyncMock()
    coordinator = mysql_adapters.RenewingLeaseCoordinator(tasks, lease_seconds=60)
    running = asyncio.create_task(
        coordinator.run(sync_task(SyncPhase.STREAMING), operation())
    )
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert cleaned.is_set()
