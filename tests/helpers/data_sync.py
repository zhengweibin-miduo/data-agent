"""Data Sync application seam 使用的可复用内存适配器。"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from typing import TypeVar

from data_agent.data_sync.application.contracts import (
    BufferedSyncEvent,
    CapturedEvents,
    ClaimedSyncTask,
    SyncPolicy,
)
from data_agent.data_sync.application.service import DataSyncService
from data_agent.data_sync.models import (
    BinlogCoordinate,
    DesiredColumn,
    DesiredSyncTable,
    RowOperation,
    SyncPhase,
    SyncRowEvent,
)

_T = TypeVar("_T")


class InMemoryTaskPort:
    """记录 application interface 产生的持久化意图。"""

    def __init__(self, claimed: Sequence[ClaimedSyncTask] = ()) -> None:
        """保存待领取任务和可观察 durable state。"""
        self.claimed = list(claimed)
        self.claim_limits: list[int] = []
        self.events: list[BufferedSyncEvent] = []
        self.captured_tails: list[BinlogCoordinate] = []
        self.cleaned: list[tuple[int, int]] = []
        self.settled: list[tuple[int, SyncPhase, float]] = []
        self.held: list[tuple[int, SyncPhase, str]] = []
        self.retried: list[tuple[int, str, int, int, int]] = []

    async def claim_tasks(
        self,
        *,
        limit: int,
        lease_seconds: int,
        max_attempts: int,
    ) -> list[ClaimedSyncTask]:
        """返回有限任务并记录领取预算。"""
        self.claim_limits.append(limit)
        return self.claimed[:limit]

    async def read_events(
        self,
        task_id: int,
        *,
        limit: int,
    ) -> list[BufferedSyncEvent]:
        """按稳定顺序返回有限待应用事件。"""
        return self.events[:limit]

    async def count_pending_events(self, task_id: int) -> int:
        """返回内存缓冲事件数。"""
        return len(self.events)

    async def record_capture(
        self,
        task: ClaimedSyncTask,
        captured: CapturedEvents,
    ) -> None:
        """原子记录捕获事件和安全尾位点。"""
        next_id = len(self.events) + 1
        self.events.extend(
            BufferedSyncEvent(id=next_id + index, event=event)
            for index, event in enumerate(captured.events)
        )
        self.captured_tails.append(captured.tail)

    async def cleanup_events(self, task_id: int, *, limit: int) -> int:
        """记录清理预算并移除内存事件。"""
        self.cleaned.append((task_id, limit))
        removed = min(limit, len(self.events))
        del self.events[:removed]
        return removed

    async def settle_phase(
        self,
        task: ClaimedSyncTask,
        phase: SyncPhase,
        *,
        delay_seconds: float = 0,
    ) -> bool:
        """记录持久阶段与数据库时钟延迟。"""
        self.settled.append((task.id, phase, delay_seconds))
        return True

    async def hold_failure(
        self,
        task: ClaimedSyncTask,
        *,
        phase: SyncPhase,
        error_type: str,
    ) -> bool:
        """记录确定性失败状态。"""
        self.held.append((task.id, phase, error_type))
        return True

    async def retry_failure(
        self,
        task: ClaimedSyncTask,
        *,
        error_type: str,
        retry_base_seconds: int,
        retry_max_seconds: int,
        max_attempts: int,
    ) -> SyncPhase | None:
        """记录有界失败退避参数。"""
        self.retried.append(
            (
                task.id,
                error_type,
                retry_base_seconds,
                retry_max_seconds,
                max_attempts,
            )
        )
        return task.phase

    async def renew_lease(
        self,
        task: ClaimedSyncTask,
        *,
        lease_seconds: int,
    ) -> bool:
        """接受内存任务续租。"""
        return True


class InMemorySource:
    """提供 application seam 所需的命名源接口。"""

    def __init__(self) -> None:
        """初始化可配置捕获与回填结果。"""
        self.captured: CapturedEvents | None = None
        self.backfill_rows: list[Mapping[str, object]] = []
        self.coordinate = BinlogCoordinate(
            file="mysql-bin.000001",
            position=4,
            row_index=0,
        )

    async def check_select_access(self, source_schema: str, source_table: str) -> None:
        """接受测试表访问。"""

    async def current_coordinate(self) -> BinlogCoordinate:
        """返回稳定测试位点。"""
        return self.coordinate

    async def capture(
        self,
        *,
        source_schema: str,
        source_table: str,
        start: BinlogCoordinate,
        limit: int,
        byte_limit: int,
    ) -> CapturedEvents:
        """返回配置结果或无事件安全尾位点。"""
        return self.captured or CapturedEvents(events=(), tail=start)

    async def read_backfill_batch(
        self,
        desired: DesiredSyncTable,
        *,
        after_key: Sequence[object] | None,
        limit: int,
    ) -> list[Mapping[str, object]]:
        """返回有限回填批次。"""
        return self.backfill_rows[:limit]


class InMemoryMaterialization:
    """记录事务型 materialization port 的 durable outcome。"""

    def __init__(self) -> None:
        """初始化物化结果。"""
        self.replayed: list[tuple[int, int, SyncPhase]] = []
        self.buffered: list[tuple[int, int]] = []
        self.backfills: list[tuple[int, tuple[Mapping[str, object], ...], float]] = []
        self.schema_error: Exception | None = None
        self.resets: list[tuple[int, BinlogCoordinate, int]] = []

    async def apply_replay_event(
        self,
        task: ClaimedSyncTask,
        event: BufferedSyncEvent,
        *,
        cleanup_limit: int,
    ) -> None:
        """记录事件及应用后持久阶段。"""
        self.replayed.append((task.id, event.id, SyncPhase.REPLAYING))

    async def apply_buffered_event(
        self,
        task: ClaimedSyncTask,
        event: BufferedSyncEvent,
    ) -> None:
        """记录饱和回填期间独立提交的事件。"""
        self.buffered.append((task.id, event.id))

    async def apply_backfill_batch(
        self,
        task: ClaimedSyncTask,
        rows: Sequence[Mapping[str, object]],
        *,
        delay_seconds: float,
    ) -> None:
        """记录原子写入和持久节流结果。"""
        self.backfills.append((task.id, tuple(rows), delay_seconds))

    async def synchronize_schema(self, task: ClaimedSyncTask) -> None:
        """完成结构同步或抛出配置的错误。"""
        if self.schema_error is not None:
            raise self.schema_error

    async def reset_generation(
        self,
        task: ClaimedSyncTask,
        coordinate: BinlogCoordinate,
        *,
        limit: int,
    ) -> None:
        """记录有界 generation 重建输入。"""
        self.resets.append((task.id, coordinate, limit))


class ImmediateLeaseCoordinator:
    """直接执行测试操作而不等待真实时钟。"""

    def __init__(self) -> None:
        """初始化续租记录。"""
        self.renewed: list[int] = []

    async def run(self, task: ClaimedSyncTask, operation: Awaitable[_T]) -> _T:
        """执行并返回操作结果。"""
        return await operation

    async def renew(self, task: ClaimedSyncTask) -> None:
        """记录显式续租。"""
        self.renewed.append(task.id)


def sync_task(
    phase: SyncPhase,
    *,
    captured: BinlogCoordinate | None = None,
) -> ClaimedSyncTask:
    """构造一个可执行的应用任务。"""
    desired = DesiredSyncTable(
        source="local",
        source_schema="business",
        source_table="fact_order",
        target_table="fact_order",
        columns=[DesiredColumn(id="id", name="id", data_type="BIGINT", nullable=False)],
        primary_key=["id"],
        schema_fingerprint="a" * 64,
        metric_dependency_column_ids=[],
    )
    return ClaimedSyncTask(
        id=1,
        desired=desired,
        desired_hash=desired.desired_hash(),
        phase=phase,
        lease_token="a" * 32,
        attempts=0,
        snapshot=None,
        captured=captured,
        applied=None,
        last_backfill_key=None,
    )


def buffered_event(task: ClaimedSyncTask, event_id: int) -> BufferedSyncEvent:
    """构造一个有序待应用事件。"""
    coordinate = BinlogCoordinate(
        file="mysql-bin.000001",
        position=120 + event_id,
        row_index=0,
    )
    return BufferedSyncEvent(
        id=event_id,
        event=SyncRowEvent(
            source=task.desired.source,
            source_schema=task.desired.source_schema,
            source_table=task.desired.source_table,
            coordinate=coordinate,
            operation=RowOperation.INSERT,
            after={"id": event_id},
        ),
    )


def sync_policy(
    *,
    event_buffer_limit: int = 1000,
    event_cleanup_batch_size: int = 100,
    backfill_interval_seconds: float = 0,
    retry_base_seconds: int = 1,
) -> SyncPolicy:
    """构造 application seam 使用的有界策略。"""
    return SyncPolicy(
        claim_lease_seconds=30,
        max_attempts=3,
        event_cleanup_batch_size=event_cleanup_batch_size,
        event_buffer_limit=event_buffer_limit,
        backfill_batch_size=100,
        backfill_interval_seconds=backfill_interval_seconds,
        poll_interval_seconds=1,
        retry_base_seconds=retry_base_seconds,
        retry_max_seconds=60,
    )


def build_in_memory_service(
    *,
    tasks: InMemoryTaskPort,
    materialization: InMemoryMaterialization,
    source: InMemorySource | None = None,
    leases: ImmediateLeaseCoordinator | None = None,
    policy: SyncPolicy | None = None,
) -> DataSyncService:
    """组合 application seam 使用的内存适配器。"""
    return DataSyncService(
        tasks=tasks,
        sources={"local": source or InMemorySource()},
        materialization=materialization,
        leases=leases or ImmediateLeaseCoordinator(),
        policy=policy or sync_policy(),
    )
