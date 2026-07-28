"""DW 结构、历史回填与 Binlog 回放状态机。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from typing import Awaitable, TypeVar

from loguru import logger

from data_agent.data_sync.backfill import (
    apply_backfill_batch,
    apply_buffered_event,
    read_backfill_batch,
    reset_source_rows,
)
from data_agent.data_sync.binlog import MySQLSourceClient
from data_agent.data_sync.models import SyncPhase
from data_agent.data_sync.repository import ClaimedSyncTask, DataSyncRepository
from data_agent.data_sync.schema_sync import DWSchemaSynchronizer
from data_agent.errors import DataAgentError
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.settings import DataSyncSettings

_T = TypeVar("_T")


class LeaseLostError(RuntimeError):
    """任务在长步骤执行期间失去租约所有权。"""


class DataSyncService:
    """执行可租约恢复的数据同步任务。"""

    def __init__(
        self,
        sources: Mapping[str, MySQLSourceClient],
        settings: DataSyncSettings,
    ) -> None:
        """保存命名源客户端和有界运行参数。"""
        self._sources = sources
        self._settings = settings

    async def dispatch_once(self) -> int:
        """领取并各执行一个有界任务步骤。"""
        async with MySQLDatabase.session() as session:
            tasks = await DataSyncRepository(session).claim_tasks(
                limit=max(1, len(self._sources)),
                lease_seconds=self._settings.claim_lease_seconds,
                max_attempts=self._settings.max_attempts,
            )
        for task in tasks:
            await self._process_safely(task)
        return len(tasks)

    async def _process_safely(self, task: ClaimedSyncTask) -> None:
        """分类一次任务失败并持久化退避、冲突或暂停。"""
        try:
            await self._process(task)
        except LeaseLostError:
            # 另一 worker 已取得所有权；旧持有者不得再尝试结算或消耗重试预算。
            logger.warning("DW 增量同步任务租约已失效，当前步骤已安全让渡")
        except DataAgentError as error:
            if error.code == "dw_primary_key_conflict":
                logger.warning("DW 增量同步检测到跨数据源主键冲突，任务已暂停等待处理")
                await self._hold(task, SyncPhase.CONFLICT, error.code)
            elif error.retryable:
                logger.warning("DW 增量同步遇到暂时业务错误，任务将在退避后重试")
                await self._retry(task, type(error).__name__)
            else:
                logger.warning("DW 增量同步遇到确定性业务错误，任务已暂停等待处理")
                await self._hold(task, SyncPhase.PAUSED, error.code)
        except (ConnectionError, OSError, TimeoutError):
            logger.warning("DW 增量同步连接失败，任务将在退避后重试")
            await self._retry(task, "source_transport_error")
        except Exception:
            logger.error("DW 增量同步发生未分类系统错误，任务将在有界退避后重试")
            await self._retry(task, "unexpected_sync_error")

    async def _process(self, task: ClaimedSyncTask) -> None:
        """按当前阶段执行一个短步骤。"""
        source = self._sources.get(task.desired.source)
        if source is None:
            raise DataAgentError(
                "unknown_data_source",
                "data_sync",
                "同步任务引用了未配置的数据源",
                details={"source": task.desired.source},
            )
        if task.phase == SyncPhase.PENDING_SCHEMA:
            await self._synchronize_schema(task)
            return
        if task.phase == SyncPhase.BUFFERING:
            coordinate = await self._with_lease_heartbeat(
                task, source.current_coordinate()
            )
            async with MySQLDatabase.session() as session:
                repository = DataSyncRepository(session)
                if not await repository.has_authority(task):
                    raise LeaseLostError("建立新基线前同步任务租约已失效")
                await reset_source_rows(
                    session, task, dw_database=self._settings.dw_database
                )
                if not await repository.record_snapshot(task, coordinate):
                    return
                if not await repository.advance_captured_coordinate(task, coordinate):
                    raise RuntimeError("初始化 Binlog 捕获位点失败")
                await repository.settle_phase(task, SyncPhase.BACKFILLING)
            return
        if task.phase == SyncPhase.BACKFILLING:
            await self._with_lease_heartbeat(task, self._capture(task, source))
            rows = await self._with_lease_heartbeat(
                task,
                read_backfill_batch(
                    source.engine,
                    task.desired,
                    after_key=task.last_backfill_key,
                    limit=self._settings.backfill_batch_size,
                ),
            )
            async with MySQLDatabase.session() as session:
                if rows:
                    await self._with_lease_heartbeat(
                        task,
                        apply_backfill_batch(
                            session,
                            task,
                            rows,
                            dw_database=self._settings.dw_database,
                        ),
                    )
                    await DataSyncRepository(session).settle_phase(
                        task, SyncPhase.BACKFILLING
                    )
                else:
                    await DataSyncRepository(session).settle_phase(
                        task, SyncPhase.REPLAYING
                    )
            if rows and self._settings.backfill_interval_seconds:
                await asyncio.sleep(self._settings.backfill_interval_seconds)
            return
        if task.phase in (SyncPhase.REPLAYING, SyncPhase.STREAMING):
            await self._with_lease_heartbeat(task, self._capture(task, source))
            async with MySQLDatabase.session() as session:
                events = await DataSyncRepository(session).read_events(task.id, limit=1)
            if events:
                async with MySQLDatabase.session() as session:
                    await self._with_lease_heartbeat(
                        task,
                        apply_buffered_event(
                            session,
                            task,
                            events[0],
                            dw_database=self._settings.dw_database,
                        ),
                    )
                    repository = DataSyncRepository(session)
                    await repository.cleanup_events(
                        task.id,
                        limit=self._settings.event_cleanup_batch_size,
                    )
                    await repository.settle_phase(task, task.phase)
                return
            next_phase = (
                SyncPhase.STREAMING if task.phase == SyncPhase.REPLAYING else task.phase
            )
            async with MySQLDatabase.session() as session:
                repository = DataSyncRepository(session)
                await repository.cleanup_events(
                    task.id,
                    limit=self._settings.event_cleanup_batch_size,
                )
                await repository.settle_phase(
                    task,
                    next_phase,
                    delay_seconds=(
                        self._settings.poll_interval_seconds
                        if next_phase == SyncPhase.STREAMING
                        else 0
                    ),
                )

    async def _synchronize_schema(self, task: ClaimedSyncTask) -> None:
        """应用 DW 安全结构演进并切换到位点捕获阶段。"""
        async with MySQLDatabase.session() as session:
            await self._with_lease_heartbeat(
                task,
                DWSchemaSynchronizer(
                    session,
                    database=self._settings.dw_database,
                ).synchronize(
                    task.desired,
                    before_ddl=lambda: self._has_authority(task),
                ),
            )
            await DataSyncRepository(session).settle_phase(task, SyncPhase.BUFFERING)

    async def _has_authority(self, task: ClaimedSyncTask) -> bool:
        """在不可逆 DDL 之前重新读取 generation 与租约权威。"""
        async with MySQLDatabase.session() as session:
            return await DataSyncRepository(session).has_authority(task)

    async def _capture(
        self,
        task: ClaimedSyncTask,
        source: MySQLSourceClient,
    ) -> None:
        """从已捕获位点追加有限事件并推进独立捕获游标。"""
        start = task.captured or task.snapshot
        if start is None:
            raise RuntimeError("同步任务缺少 Binlog 起始位点")
        async with MySQLDatabase.session() as session:
            repository = DataSyncRepository(session)
            pending = await repository.count_pending_events(task.id)
        remaining = self._settings.event_buffer_limit - pending
        if remaining <= 0:
            return
        captured = await source.capture(
            source_schema=task.desired.source_schema,
            source_table=task.desired.source_table,
            start=start,
            limit=remaining,
        )
        async with MySQLDatabase.session() as session:
            repository = DataSyncRepository(session)
            for event in captured.events:
                await repository.append_event(task.id, event)
            if not await repository.advance_captured_coordinate(task, captured.tail):
                raise RuntimeError("持久化 Binlog 捕获位点时任务租约已失效")

    async def _retry(self, task: ClaimedSyncTask, error_type: str) -> None:
        """持久化一次有界指数退避。"""
        async with MySQLDatabase.session() as session:
            phase = await DataSyncRepository(session).retry_failure(
                task,
                error_type=error_type,
                retry_base_seconds=self._settings.retry_base_seconds,
                retry_max_seconds=self._settings.retry_max_seconds,
                max_attempts=self._settings.max_attempts,
            )
        if phase == SyncPhase.DEAD:
            logger.error("DW 增量同步重试预算耗尽，任务已进入死信等待处理")

    async def _with_lease_heartbeat(
        self,
        task: ClaimedSyncTask,
        operation: Awaitable[_T],
    ) -> _T:
        """执行长步骤，并按租期三分之一使用数据库时钟续租。"""
        running = asyncio.ensure_future(operation)
        interval = max(0.1, self._settings.claim_lease_seconds / 3)
        try:
            while True:
                done, _ = await asyncio.wait({running}, timeout=interval)
                if done:
                    return await running
                async with MySQLDatabase.session() as session:
                    renewed = await DataSyncRepository(session).renew_lease(
                        task.id,
                        task.lease_token,
                        lease_seconds=self._settings.claim_lease_seconds,
                    )
                if not renewed:
                    running.cancel()
                    with suppress(asyncio.CancelledError):
                        await running
                    raise LeaseLostError("同步任务租约已失效")
        except BaseException:
            if not running.done():
                running.cancel()
            raise

    async def _hold(
        self,
        task: ClaimedSyncTask,
        phase: SyncPhase,
        error_type: str,
    ) -> None:
        """保留确定性暂停或冲突状态。"""
        async with MySQLDatabase.session() as session:
            await DataSyncRepository(session).hold_failure(
                task,
                phase=phase,
                error_type=error_type,
            )
