"""Data Sync 应用层中立值与驱动端口。"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar

from data_agent.data_sync.models import (
    BinlogCoordinate,
    DesiredSyncTable,
    SyncPhase,
    SyncRowEvent,
)

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class ClaimedSyncTask:
    """持有短期租约的数据同步任务投影。"""

    id: int
    desired: DesiredSyncTable
    desired_hash: str
    phase: SyncPhase
    lease_token: str
    attempts: int
    snapshot: BinlogCoordinate | None
    captured: BinlogCoordinate | None
    applied: BinlogCoordinate | None
    last_backfill_key: tuple[object, ...] | None


@dataclass(frozen=True, slots=True)
class BufferedSyncEvent:
    """待回放的持久化 Binlog 行事件。"""

    id: int
    event: SyncRowEvent


@dataclass(frozen=True, slots=True)
class CapturedEvents:
    """一次有界源捕获的持久化事件与安全尾位点。"""

    events: tuple[SyncRowEvent, ...]
    tail: BinlogCoordinate


@dataclass(frozen=True, slots=True)
class SyncPolicy:
    """Data Sync 应用编排所需的有界策略值。"""

    claim_lease_seconds: int
    max_attempts: int
    event_cleanup_batch_size: int
    event_buffer_limit: int
    backfill_batch_size: int
    backfill_interval_seconds: float
    poll_interval_seconds: float
    retry_base_seconds: int
    retry_max_seconds: int


class LeaseLostError(RuntimeError):
    """任务在长步骤执行期间失去租约所有权。"""


class SyncResourceBusyError(RuntimeError):
    """同步所需的 generation 或 schema 资源暂时被占用。"""


class SyncTaskPort(Protocol):
    """Data Sync 应用使用的持久任务与事件接口。"""

    async def claim_tasks(
        self,
        *,
        limit: int,
        lease_seconds: int,
        max_attempts: int,
    ) -> list[ClaimedSyncTask]:
        """使用数据库时钟领取有限任务。"""
        ...

    async def read_events(
        self,
        task_id: int,
        *,
        limit: int,
    ) -> list[BufferedSyncEvent]:
        """按稳定坐标顺序读取有限待应用事件。"""
        ...

    async def count_pending_events(self, task_id: int) -> int:
        """返回任务尚未确认的事件数量。"""
        ...

    async def record_capture(
        self,
        task: ClaimedSyncTask,
        captured: CapturedEvents,
    ) -> None:
        """原子持久化捕获事件、尾位点和就绪阶段变化。"""
        ...

    async def cleanup_events(self, task_id: int, *, limit: int) -> int:
        """有界删除已确认事件。"""
        ...

    async def settle_phase(
        self,
        task: ClaimedSyncTask,
        phase: SyncPhase,
        *,
        delay_seconds: float = 0,
    ) -> bool:
        """按任务权威身份持久化下一阶段和数据库时钟延迟。"""
        ...

    async def retry_failure(
        self,
        task: ClaimedSyncTask,
        *,
        error_type: str,
        retry_base_seconds: int,
        retry_max_seconds: int,
        max_attempts: int,
    ) -> SyncPhase | None:
        """按数据库时钟持久化有界退避或死信状态。"""
        ...

    async def hold_failure(
        self,
        task: ClaimedSyncTask,
        *,
        phase: SyncPhase,
        error_type: str,
    ) -> bool:
        """持久化确定性暂停或冲突。"""
        ...

    async def renew_lease(
        self,
        task: ClaimedSyncTask,
        *,
        lease_seconds: int,
    ) -> bool:
        """使用数据库时钟延长当前任务租约。"""
        ...


class SourcePort(Protocol):
    """Data Sync 应用使用的命名源读取接口。"""

    async def check_select_access(self, source_schema: str, source_table: str) -> None:
        """确认源表可读取。"""
        ...

    async def current_coordinate(self) -> BinlogCoordinate:
        """读取当前安全 Binlog 位点。"""
        ...

    async def capture(
        self,
        *,
        source_schema: str,
        source_table: str,
        start: BinlogCoordinate,
        limit: int,
        byte_limit: int,
    ) -> CapturedEvents:
        """从持久化位点捕获有界行事件。"""
        ...

    async def read_backfill_batch(
        self,
        desired: DesiredSyncTable,
        *,
        after_key: Sequence[object] | None,
        limit: int,
    ) -> list[Mapping[str, object]]:
        """按主键游标读取有限历史行。"""
        ...


class MaterializationPort(Protocol):
    """Data Sync 应用使用的事务型 DW 物化接口。"""

    async def apply_replay_event(
        self,
        task: ClaimedSyncTask,
        event: BufferedSyncEvent,
        *,
        cleanup_limit: int,
    ) -> None:
        """原子应用、清理一个事件并持久保持 replaying。"""
        ...

    async def apply_buffered_event(
        self,
        task: ClaimedSyncTask,
        event: BufferedSyncEvent,
    ) -> None:
        """在独立事务中应用饱和回填缓冲事件。"""
        ...

    async def apply_backfill_batch(
        self,
        task: ClaimedSyncTask,
        rows: Sequence[Mapping[str, object]],
        *,
        delay_seconds: float,
    ) -> None:
        """原子写入回填批次、游标、投影输入和持久节流。"""
        ...

    async def synchronize_schema(self, task: ClaimedSyncTask) -> None:
        """在 generation/schema 锁和任务权威下同步 DW 结构。"""
        ...

    async def reset_generation(
        self,
        task: ClaimedSyncTask,
        coordinate: BinlogCoordinate,
        *,
        limit: int,
    ) -> None:
        """有界清理旧行并原子建立 snapshot/captured 基线。"""
        ...


class LeaseCoordinator(Protocol):
    """长步骤续租与取消清理接口。"""

    async def run(self, task: ClaimedSyncTask, operation: Awaitable[_T]) -> _T:
        """在周期续租保护下执行一个长步骤。"""
        ...

    async def renew(self, task: ClaimedSyncTask) -> None:
        """在相邻短事务之间显式续租。"""
        ...
