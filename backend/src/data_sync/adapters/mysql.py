"""Data Sync 任务、DW 物化和租约的 MySQL 适配器。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from data_sync.application.contracts import (
    BufferedSyncEvent,
    CapturedEvents,
    ClaimedSyncTask,
    LeaseLostError,
    SyncResourceBusyError,
    SyncTaskPort,
)
from data_sync.backfill import (
    apply_backfill_batch,
    apply_buffered_event,
    reset_source_rows,
)
from data_sync.locks import generation_lock_name
from data_sync.models import (
    BinlogCoordinate,
    DesiredSyncTable,
    SyncPhase,
)
from data_sync.repository import DataSyncRepository
from data_sync.schema_sync import (
    DWSchemaSynchronizer,
    SchemaLockUnavailableError,
)
from ddl_metadata.meta_projection.application.value_input import (
    ValueProjectionParticipant,
)
from infrastructure.mysql import (
    AdvisoryLockUnavailableError,
    MySQLDatabase,
)
from settings import DataSyncSettings

_T = TypeVar("_T")
ValueProjectionFactory = Callable[
    [AsyncSession, DesiredSyncTable],
    ValueProjectionParticipant,
]


class MySQLSyncTaskAdapter:
    """以短事务实现 durable task 和 event-buffer port。"""

    async def claim_tasks(
        self,
        *,
        limit: int,
        lease_seconds: int,
        max_attempts: int,
    ) -> list[ClaimedSyncTask]:
        """领取有限任务并提交数据库时钟租约。"""
        async with MySQLDatabase.session() as session:
            return await DataSyncRepository(session).claim_tasks(
                limit=limit,
                lease_seconds=lease_seconds,
                max_attempts=max_attempts,
            )

    async def read_events(
        self,
        task_id: int,
        *,
        limit: int,
    ) -> list[BufferedSyncEvent]:
        """读取有限待应用事件。"""
        async with MySQLDatabase.session() as session:
            return await DataSyncRepository(session).read_events(task_id, limit=limit)

    async def count_pending_events(self, task_id: int) -> int:
        """读取任务当前事件缓冲数量。"""
        async with MySQLDatabase.session() as session:
            return await DataSyncRepository(session).count_pending_events(task_id)

    async def record_capture(
        self,
        task: ClaimedSyncTask,
        captured: CapturedEvents,
    ) -> None:
        """原子写入捕获事件、尾位点和 streaming readiness 变化。"""
        async with MySQLDatabase.session() as session:
            repository = DataSyncRepository(session)
            await repository.append_events(task.id, captured.events)
            if not await repository.advance_captured_coordinate(
                task,
                captured.tail,
                has_new_events=bool(captured.events),
            ):
                raise LeaseLostError("持久化 Binlog 捕获位点时任务租约已失效")

    async def cleanup_events(self, task_id: int, *, limit: int) -> int:
        """有界删除已确认事件。"""
        async with MySQLDatabase.session() as session:
            return await DataSyncRepository(session).cleanup_events(
                task_id, limit=limit
            )

    async def settle_phase(
        self,
        task: ClaimedSyncTask,
        phase: SyncPhase,
        *,
        delay_seconds: float = 0,
    ) -> bool:
        """按完整任务权威持久化下一阶段。"""
        async with MySQLDatabase.session() as session:
            return await DataSyncRepository(session).settle_phase(
                task,
                phase,
                delay_seconds=delay_seconds,
            )

    async def retry_failure(
        self,
        task: ClaimedSyncTask,
        *,
        error_type: str,
        retry_base_seconds: int,
        retry_max_seconds: int,
        max_attempts: int,
    ) -> SyncPhase | None:
        """持久化数据库时钟退避或死信状态。"""
        async with MySQLDatabase.session() as session:
            return await DataSyncRepository(session).retry_failure(
                task,
                error_type=error_type,
                retry_base_seconds=retry_base_seconds,
                retry_max_seconds=retry_max_seconds,
                max_attempts=max_attempts,
            )

    async def hold_failure(
        self,
        task: ClaimedSyncTask,
        *,
        phase: SyncPhase,
        error_type: str,
    ) -> bool:
        """持久化确定性暂停或冲突。"""
        async with MySQLDatabase.session() as session:
            return await DataSyncRepository(session).hold_failure(
                task,
                phase=phase,
                error_type=error_type,
            )

    async def renew_lease(
        self,
        task: ClaimedSyncTask,
        *,
        lease_seconds: int,
    ) -> bool:
        """仅以当前 token 续租任务。"""
        async with MySQLDatabase.session() as session:
            return await DataSyncRepository(session).renew_lease(
                task.id,
                task.lease_token,
                lease_seconds=lease_seconds,
            )

    async def read_desired_tables(self) -> list[DesiredSyncTable]:
        """读取 worker 启动权限探测所需源表。"""
        async with MySQLDatabase.session() as session:
            return await DataSyncRepository(session).read_desired_tables()


class MySQLMaterializationAdapter:
    """隐藏 DW SQL、Session、锁和 Meta Projection 事务参与者。"""

    def __init__(
        self,
        settings: DataSyncSettings,
        projection_factory: ValueProjectionFactory,
    ) -> None:
        """保存物化配置和 composition root 选择的 projection adapter。"""
        self._settings = settings
        self._projection_factory = projection_factory

    async def synchronize_schema(self, task: ClaimedSyncTask) -> None:
        """在 generation 锁内同步结构并持久化 BUFFERING。"""
        lock_name = generation_lock_name(
            self._settings.dw_database,
            task.desired.target_table,
        )
        try:
            # 步骤一：generation lock 跨越 DDL Session 提交，保持全局锁顺序。
            async with MySQLDatabase.exclusive_service_locks(
                [lock_name],
                timeout_seconds=self._settings.generation_lock_timeout_seconds,
            ):
                async with MySQLDatabase.session() as session:
                    await session.connection(
                        execution_options={"isolation_level": "READ COMMITTED"}
                    )
                    repository = DataSyncRepository(session)
                    # 步骤二：provenance 使用独立一致性快照，避免阻塞本次 DDL。
                    async with MySQLDatabase.session() as provenance_session:
                        await provenance_session.connection(
                            execution_options={"isolation_level": "REPEATABLE READ"}
                        )
                        await (
                            DWSchemaSynchronizer(
                                session,
                                database=self._settings.dw_database,
                            )
                            .with_provenance_session(provenance_session)
                            .synchronize(
                                task.desired,
                                check_authority=lambda: repository.has_authority(task),
                            )
                        )
                    # 步骤三：settlement 与 managed commit 完成后，
                    # 才释放 generation lock。
                    if not await repository.settle_phase(task, SyncPhase.BUFFERING):
                        raise LeaseLostError("完成 DW 结构同步后任务租约已失效")
        except (AdvisoryLockUnavailableError, SchemaLockUnavailableError) as error:
            raise SyncResourceBusyError("DW generation 或 schema 资源被占用") from error

    async def reset_generation(
        self,
        task: ClaimedSyncTask,
        coordinate: BinlogCoordinate,
        *,
        limit: int,
    ) -> None:
        """在 generation lock 内清理旧代次，避免与只读查询交错。"""
        lock_name = generation_lock_name(
            self._settings.dw_database,
            task.desired.target_table,
        )
        try:
            async with MySQLDatabase.exclusive_service_locks(
                [lock_name],
                timeout_seconds=self._settings.generation_lock_timeout_seconds,
            ):
                await self._reset_generation(task, coordinate, limit=limit)
        except AdvisoryLockUnavailableError as error:
            raise SyncResourceBusyError("DW generation 资源被占用") from error

    async def _reset_generation(
        self,
        task: ClaimedSyncTask,
        coordinate: BinlogCoordinate,
        *,
        limit: int,
    ) -> None:
        """在一个事务中有界清理旧 generation 并建立新基线。"""
        async with MySQLDatabase.session() as session:
            repository = DataSyncRepository(session)
            if not await repository.has_authority(task):
                raise LeaseLostError("建立新基线前同步任务租约已失效")
            reset_complete = await reset_source_rows(
                session,
                task,
                dw_database=self._settings.dw_database,
                limit=limit,
                value_projection=self._projection_factory(session, task.desired),
            )
            if not reset_complete:
                if not await repository.settle_phase(task, SyncPhase.BUFFERING):
                    raise LeaseLostError("分批清理旧行后同步任务租约已失效")
                return
            if not await repository.record_snapshot(task, coordinate):
                raise LeaseLostError("清理旧物化行后同步任务租约已失效")
            if not await repository.advance_captured_coordinate(task, coordinate):
                raise LeaseLostError("初始化 Binlog 捕获位点时同步任务租约已失效")
            if not await repository.settle_phase(task, SyncPhase.BACKFILLING):
                raise LeaseLostError("建立新基线后同步任务租约已失效")

    async def apply_buffered_event(
        self,
        task: ClaimedSyncTask,
        event: BufferedSyncEvent,
    ) -> None:
        """在独立事务中应用一个饱和回填事件。"""
        async with MySQLDatabase.session() as session:
            await apply_buffered_event(
                session,
                task,
                event,
                dw_database=self._settings.dw_database,
                value_projection=self._projection_factory(session, task.desired),
            )

    async def apply_backfill_batch(
        self,
        task: ClaimedSyncTask,
        rows: Sequence[Mapping[str, object]],
        *,
        delay_seconds: float,
    ) -> None:
        """原子写入回填、游标、projection input 和持久节流。"""
        async with MySQLDatabase.session() as session:
            await apply_backfill_batch(
                session,
                task,
                rows,
                dw_database=self._settings.dw_database,
                value_projection=self._projection_factory(session, task.desired),
            )
            if not await DataSyncRepository(session).settle_phase(
                task,
                SyncPhase.BACKFILLING,
                delay_seconds=delay_seconds,
            ):
                raise LeaseLostError("回填批次完成后同步任务租约已失效")

    async def apply_replay_event(
        self,
        task: ClaimedSyncTask,
        event: BufferedSyncEvent,
        *,
        cleanup_limit: int,
    ) -> None:
        """原子应用事件、推进坐标、清理并保持 REPLAYING。"""
        async with MySQLDatabase.session() as session:
            await apply_buffered_event(
                session,
                task,
                event,
                dw_database=self._settings.dw_database,
                value_projection=self._projection_factory(session, task.desired),
            )
            repository = DataSyncRepository(session)
            await repository.cleanup_events(task.id, limit=cleanup_limit)
            if not await repository.settle_phase(task, SyncPhase.REPLAYING):
                raise LeaseLostError("事件回放完成后同步任务租约已失效")


class RenewingLeaseCoordinator:
    """使用任务 port 和单调等待周期保护长步骤。"""

    def __init__(
        self,
        tasks: SyncTaskPort,
        *,
        lease_seconds: int,
    ) -> None:
        """保存续租端口和租期。"""
        self._tasks = tasks
        self._lease_seconds = lease_seconds

    async def run(self, task: ClaimedSyncTask, operation: Awaitable[_T]) -> _T:
        """按租期三分之一续租，并等待取消操作完成清理。"""
        running = asyncio.ensure_future(operation)
        interval = max(0.1, self._lease_seconds / 3)
        try:
            while True:
                done, _ = await asyncio.wait({running}, timeout=interval)
                if done:
                    return await running
                if not await self._tasks.renew_lease(
                    task,
                    lease_seconds=self._lease_seconds,
                ):
                    running.cancel()
                    with suppress(asyncio.CancelledError):
                        await running
                    raise LeaseLostError("同步任务租约已失效")
        except BaseException:
            if not running.done():
                running.cancel()
                with suppress(asyncio.CancelledError):
                    await running
            raise

    async def renew(self, task: ClaimedSyncTask) -> None:
        """在短事务之间显式续租。"""
        if not await self._tasks.renew_lease(
            task,
            lease_seconds=self._lease_seconds,
        ):
            raise LeaseLostError("同步任务租约已失效")
