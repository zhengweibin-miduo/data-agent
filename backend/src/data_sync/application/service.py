"""通过中立端口执行 Data Sync 有界任务步骤。"""

from __future__ import annotations

from collections.abc import Mapping
from types import TracebackType

from loguru import logger

from data_sync.application.contracts import (
    ClaimedSyncTask,
    LeaseCoordinator,
    LeaseLostError,
    MaterializationPort,
    SourcePort,
    SyncPolicy,
    SyncResourceBusyError,
    SyncTaskPort,
)
from data_sync.models import SyncPhase
from errors import DataAgentError


class DataSyncService:
    """隐藏阶段、事务、坐标和租约细节的 Data Sync 深模块。"""

    def __init__(
        self,
        *,
        tasks: SyncTaskPort,
        sources: Mapping[str, SourcePort],
        materialization: MaterializationPort,
        leases: LeaseCoordinator,
        policy: SyncPolicy,
    ) -> None:
        """保存应用端口和有界策略。"""
        self._tasks = tasks
        self._sources = sources
        self._materialization = materialization
        self._leases = leases
        self._policy = policy

    async def dispatch_once(self) -> int:
        """领取并各执行一个有界任务步骤。"""
        claimed = await self._tasks.claim_tasks(
            limit=1,
            lease_seconds=self._policy.claim_lease_seconds,
            max_attempts=self._policy.max_attempts,
        )
        for task in claimed:
            await self._process_safely(task)
        return len(claimed)

    async def _process_safely(self, task: ClaimedSyncTask) -> None:
        """把一次失败收敛为安全持久状态。"""
        try:
            await self._process(task)
        except LeaseLostError:
            logger.warning("DW 增量同步任务租约已失效，当前步骤已安全让渡")
        except SyncResourceBusyError:
            await self._tasks.settle_phase(
                task,
                task.phase,
                delay_seconds=self._policy.retry_base_seconds,
            )
        except DataAgentError as error:
            if error.code == "dw_primary_key_conflict":
                await self._tasks.hold_failure(
                    task,
                    phase=SyncPhase.CONFLICT,
                    error_type=error.code,
                )
            elif error.retryable:
                await self._retry(task, type(error).__name__)
            else:
                await self._tasks.hold_failure(
                    task,
                    phase=SyncPhase.PAUSED,
                    error_type=error.code,
                )
        except (ConnectionError, OSError, TimeoutError):
            await self._retry(task, "source_transport_error")
        except Exception as error:
            logger.opt(exception=_redacted_exception(error)).error(
                f"DW 增量同步任务 {task.id} 在 {task.phase.value} 阶段发生"
                "未分类系统错误，"
                f"来源 {task.desired.source}、目标表 {task.desired.target_table} "
                "将在有界退避后重试"
            )
            await self._retry(
                task,
                f"unexpected_sync_error:{task.phase.value}:{type(error).__name__}",
            )

    async def _retry(self, task: ClaimedSyncTask, error_type: str) -> None:
        """持久化一次有界失败退避。"""
        phase = await self._tasks.retry_failure(
            task,
            error_type=error_type,
            retry_base_seconds=self._policy.retry_base_seconds,
            retry_max_seconds=self._policy.retry_max_seconds,
            max_attempts=self._policy.max_attempts,
        )
        if phase is SyncPhase.DEAD:
            logger.error("DW 增量同步重试预算耗尽，任务已进入死信等待处理")

    async def _process(self, task: ClaimedSyncTask) -> None:
        """按当前阶段执行一个有界步骤。"""
        source = self._sources.get(task.desired.source)
        if source is None:
            raise DataAgentError(
                "unknown_data_source",
                "data_sync",
                "同步任务引用了未配置的数据源",
                details={"source": task.desired.source},
            )
        if task.phase is SyncPhase.PENDING_SCHEMA:
            await source.check_select_access(
                task.desired.source_schema,
                task.desired.source_table,
            )
            await self._leases.run(
                task,
                self._materialization.synchronize_schema(task),
            )
            return
        if task.phase is SyncPhase.BUFFERING:
            coordinate = await self._leases.run(
                task,
                source.current_coordinate(),
            )
            await self._leases.run(
                task,
                self._materialization.reset_generation(
                    task,
                    coordinate,
                    limit=self._policy.backfill_batch_size,
                ),
            )
            return
        if task.phase in (SyncPhase.REPLAYING, SyncPhase.STREAMING):
            events = await self._tasks.read_events(task.id, limit=1)
            if not events:
                await self._leases.run(task, self._capture(task, source))
                events = await self._tasks.read_events(task.id, limit=1)
            if events:
                await self._leases.run(
                    task,
                    self._materialization.apply_replay_event(
                        task,
                        events[0],
                        cleanup_limit=self._policy.event_cleanup_batch_size,
                    ),
                )
            else:
                await self._tasks.cleanup_events(
                    task.id,
                    limit=self._policy.event_cleanup_batch_size,
                )
                next_phase = (
                    SyncPhase.STREAMING
                    if task.phase is SyncPhase.REPLAYING
                    else task.phase
                )
                await self._tasks.settle_phase(
                    task,
                    next_phase,
                    delay_seconds=(
                        self._policy.poll_interval_seconds
                        if next_phase is SyncPhase.STREAMING
                        else 0
                    ),
                )
            return
        if task.phase is SyncPhase.BACKFILLING:
            capture_has_capacity = await self._leases.run(
                task,
                self._capture(task, source),
            )
            if not capture_has_capacity:
                events = await self._tasks.read_events(
                    task.id,
                    limit=self._policy.event_cleanup_batch_size,
                )
                if not events:
                    raise RuntimeError("Binlog 缓冲已饱和但没有待应用事件")
                # 步骤一：每个事件独立提交，事件之间续租，避免心跳等待任务行锁。
                for event in events:
                    await self._leases.renew(task)
                    await self._materialization.apply_buffered_event(task, event)
                await self._tasks.cleanup_events(
                    task.id,
                    limit=self._policy.event_cleanup_batch_size,
                )
            # 步骤二：从持久化游标读取一批源行，不因缓冲饱和重置 generation。
            rows = await self._leases.run(
                task,
                source.read_backfill_batch(
                    task.desired,
                    after_key=task.last_backfill_key,
                    limit=self._policy.backfill_batch_size,
                ),
            )
            if rows:
                await self._leases.run(
                    task,
                    self._materialization.apply_backfill_batch(
                        task,
                        rows,
                        delay_seconds=self._policy.backfill_interval_seconds,
                    ),
                )
            else:
                await self._tasks.settle_phase(task, SyncPhase.REPLAYING)

    async def _capture(self, task: ClaimedSyncTask, source: SourcePort) -> bool:
        """捕获有界事件并返回缓冲是否仍有容量。"""
        start = task.captured or task.snapshot
        if start is None:
            raise RuntimeError("同步任务缺少 Binlog 起始位点")
        pending = await self._tasks.count_pending_events(task.id)
        remaining = self._policy.event_buffer_limit - pending
        if remaining <= 0:
            return False
        captured = await source.capture(
            source_schema=task.desired.source_schema,
            source_table=task.desired.source_table,
            start=start,
            limit=min(remaining, 1000),
            byte_limit=1024 * 1024,
        )
        await self._tasks.record_capture(task, captured)
        return pending + len(captured.events) < self._policy.event_buffer_limit


def _redacted_exception(
    error: Exception,
) -> tuple[type[RuntimeError], RuntimeError, TracebackType | None]:
    """保留调用栈和原异常类型，同时从日志中移除异常消息。"""
    safe_error = RuntimeError(f"{type(error).__name__}: 异常详情已脱敏")
    return RuntimeError, safe_error, error.__traceback__
