"""Data Sync application interface 行为检查。"""

import pytest
from tests.helpers.data_sync import (
    ImmediateLeaseCoordinator,
    InMemoryMaterialization,
    InMemorySource,
    InMemoryTaskPort,
    buffered_event,
    build_in_memory_service,
    sync_policy,
    sync_task,
)

from data_sync.application.contracts import (
    BufferedSyncEvent,
    CapturedEvents,
    SyncPolicy,
    SyncResourceBusyError,
)
from data_sync.application.service import DataSyncService
from data_sync.models import (
    BinlogCoordinate,
    RowOperation,
    SyncPhase,
    SyncRowEvent,
)
from errors import DataAgentError


async def test_dispatch_once_claims_at_most_one_task() -> None:
    """串行 application interface 每次最多领取一个可立即执行任务。"""
    tasks = InMemoryTaskPort()
    service = DataSyncService(
        tasks=tasks,
        sources={"local": InMemorySource()},
        materialization=InMemoryMaterialization(),
        leases=ImmediateLeaseCoordinator(),
        policy=SyncPolicy(
            claim_lease_seconds=30,
            max_attempts=3,
            event_cleanup_batch_size=100,
            event_buffer_limit=1000,
            backfill_batch_size=100,
            backfill_interval_seconds=0,
            poll_interval_seconds=1,
            retry_base_seconds=1,
            retry_max_seconds=60,
        ),
    )

    processed = await service.dispatch_once()

    assert processed == 0
    assert tasks.claim_limits == [1]


async def test_dispatch_once_applies_streaming_backlog() -> None:
    """实时积压在一次公共调用内应用，并持久保持非就绪阶段。"""
    task = sync_task(SyncPhase.STREAMING)
    coordinate = BinlogCoordinate(file="mysql-bin.000001", position=120, row_index=0)
    event = BufferedSyncEvent(
        id=7,
        event=SyncRowEvent(
            source="local",
            source_schema=task.desired.source_schema,
            source_table=task.desired.source_table,
            coordinate=coordinate,
            operation=RowOperation.INSERT,
            after={"id": 1},
        ),
    )
    tasks = InMemoryTaskPort([task])
    tasks.events = [event]
    materialization = InMemoryMaterialization()
    service = build_in_memory_service(tasks=tasks, materialization=materialization)

    processed = await service.dispatch_once()

    assert processed == 1
    assert materialization.replayed == [(task.id, event.id, SyncPhase.REPLAYING)]


async def test_dispatch_once_persists_capture_before_replay() -> None:
    """实时空缓冲原子保存事件和尾位点后回放。"""
    start = BinlogCoordinate(file="mysql-bin.000001", position=120, row_index=0)
    tail = BinlogCoordinate(file="mysql-bin.000001", position=140, row_index=0)
    task = sync_task(SyncPhase.STREAMING, captured=start)
    row_event = SyncRowEvent(
        source="local",
        source_schema=task.desired.source_schema,
        source_table=task.desired.source_table,
        coordinate=tail,
        operation=RowOperation.INSERT,
        after={"id": 2},
    )
    source = InMemorySource()
    source.captured = CapturedEvents(events=(row_event,), tail=tail)
    tasks = InMemoryTaskPort([task])
    materialization = InMemoryMaterialization()
    service = build_in_memory_service(
        tasks=tasks,
        materialization=materialization,
        source=source,
    )

    await service.dispatch_once()

    assert tasks.captured_tails == [tail]
    assert [item.event for item in tasks.events] == [row_event]
    assert materialization.replayed == [(task.id, 1, SyncPhase.REPLAYING)]


async def test_dispatch_once_drains_saturated_backlog_then_finishes_backfill() -> None:
    """饱和缓冲逐事件提交并续租，随后从既有游标继续。"""
    start = BinlogCoordinate(file="mysql-bin.000001", position=100, row_index=0)
    task = sync_task(SyncPhase.BACKFILLING, captured=start)
    tasks = InMemoryTaskPort([task])
    tasks.events = [buffered_event(task, event_id) for event_id in (1, 2, 3)]
    materialization = InMemoryMaterialization()
    leases = ImmediateLeaseCoordinator()
    service = build_in_memory_service(
        tasks=tasks,
        materialization=materialization,
        leases=leases,
        policy=sync_policy(event_buffer_limit=3, event_cleanup_batch_size=3),
    )

    await service.dispatch_once()

    assert materialization.buffered == [(task.id, 1), (task.id, 2), (task.id, 3)]
    assert leases.renewed == [task.id, task.id, task.id]
    assert tasks.cleaned == [(task.id, 3)]
    assert tasks.settled == [(task.id, SyncPhase.REPLAYING, 0)]


async def test_dispatch_once_reschedules_schema_contention() -> None:
    """结构资源竞争只延后同一阶段，不进入失败退避。"""
    task = sync_task(SyncPhase.PENDING_SCHEMA)
    tasks = InMemoryTaskPort([task])
    materialization = InMemoryMaterialization()
    materialization.schema_error = SyncResourceBusyError("generation busy")
    service = build_in_memory_service(
        tasks=tasks,
        materialization=materialization,
        policy=sync_policy(retry_base_seconds=5),
    )

    await service.dispatch_once()

    assert tasks.settled == [(task.id, SyncPhase.PENDING_SCHEMA, 5)]


async def test_dispatch_once_establishes_buffering_baseline() -> None:
    """缓冲阶段从源位点建立有界且事务型的新 generation 基线。"""
    task = sync_task(SyncPhase.BUFFERING)
    source = InMemorySource()
    source.coordinate = BinlogCoordinate(
        file="mysql-bin.000009",
        position=512,
        row_index=0,
    )
    tasks = InMemoryTaskPort([task])
    materialization = InMemoryMaterialization()
    service = build_in_memory_service(
        tasks=tasks,
        materialization=materialization,
        source=source,
    )

    await service.dispatch_once()

    assert materialization.resets == [(task.id, source.coordinate, 100)]


async def test_dispatch_once_marks_replay_complete_after_empty_recapture() -> None:
    """回放仅在重新捕获仍为空后进入 streaming 并持久化轮询延迟。"""
    start = BinlogCoordinate(file="mysql-bin.000001", position=200, row_index=0)
    task = sync_task(SyncPhase.REPLAYING, captured=start)
    tasks = InMemoryTaskPort([task])
    materialization = InMemoryMaterialization()
    service = build_in_memory_service(tasks=tasks, materialization=materialization)

    await service.dispatch_once()

    assert tasks.captured_tails == [start]
    assert tasks.settled == [(task.id, SyncPhase.STREAMING, 1)]


@pytest.mark.parametrize(
    ("error", "expected_hold", "expected_retry"),
    [
        (
            DataAgentError("dw_primary_key_conflict", "data_sync", "conflict"),
            (SyncPhase.CONFLICT, "dw_primary_key_conflict"),
            None,
        ),
        (
            DataAgentError("unsafe_schema", "data_sync", "unsafe"),
            (SyncPhase.PAUSED, "unsafe_schema"),
            None,
        ),
        (
            DataAgentError(
                "source_temporarily_unavailable",
                "data_sync",
                "temporary",
                retryable=True,
            ),
            None,
            "DataAgentError",
        ),
        (ConnectionError("secret endpoint"), None, "source_transport_error"),
        (
            ValueError("sensitive row"),
            None,
            "unexpected_sync_error:pending_schema:ValueError",
        ),
    ],
)
async def test_dispatch_once_persists_safe_failure_outcome(
    error: Exception,
    expected_hold: tuple[SyncPhase, str] | None,
    expected_retry: str | None,
) -> None:
    """公共调用把失败收敛为安全冲突、暂停或有界退避状态。"""
    task = sync_task(SyncPhase.PENDING_SCHEMA)
    tasks = InMemoryTaskPort([task])
    materialization = InMemoryMaterialization()
    materialization.schema_error = error
    service = build_in_memory_service(tasks=tasks, materialization=materialization)

    await service.dispatch_once()

    assert tasks.held == (
        [(task.id, expected_hold[0], expected_hold[1])] if expected_hold else []
    )
    assert [item[1] for item in tasks.retried] == (
        [expected_retry] if expected_retry else []
    )
