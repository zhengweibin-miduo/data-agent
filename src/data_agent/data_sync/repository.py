"""data_sync 控制面状态、事件和目标主键归属仓储。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any, Sequence

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.engine import CursorResult, RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.data_sync.models import (
    BinlogCoordinate,
    DesiredSyncTable,
    KeyConflict,
    SyncPhase,
    SyncRowEvent,
    decode_row_value,
    encode_row_value,
)
from data_agent.data_sync.tables import (
    data_sync_event,
    data_sync_key_owner,
    data_sync_task,
)
from data_agent.errors import DataAgentError

_RUNNABLE_PHASES = (
    SyncPhase.PENDING_SCHEMA.value,
    SyncPhase.BUFFERING.value,
    SyncPhase.BACKFILLING.value,
    SyncPhase.REPLAYING.value,
    SyncPhase.STREAMING.value,
)


def _rowcount(result: object) -> int:
    """读取 DML 结果影响行数。"""
    return result.rowcount if isinstance(result, CursorResult) else 0


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


class DataSyncRepository:
    """在调用方事务内维护数据同步控制状态。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存调用方拥有的异步 Session。"""
        self._session = session

    async def upsert_desired(self, desired_tables: Sequence[DesiredSyncTable]) -> None:
        """幂等写入 Meta 快照派生的当前同步期望状态。"""
        for desired in desired_tables:
            await self._reject_conflicting_target(desired)
            await self._upsert_one_desired(desired)

    async def _reject_conflicting_target(self, desired: DesiredSyncTable) -> None:
        """拒绝同一命名来源跨快照复用同一 DW 目标。"""
        conflict = await self._session.scalar(
            select(data_sync_task.c.id).where(
                data_sync_task.c.source == desired.source,
                data_sync_task.c.target_table == desired.target_table,
                or_(
                    data_sync_task.c.source_schema != desired.source_schema,
                    data_sync_task.c.source_table != desired.source_table,
                ),
            ).limit(1)
        )
        if conflict is not None:
            raise DataAgentError(
                "duplicate_data_sync_target",
                "persist_snapshot",
                "同一数据源的多个物理表不能映射到同一 DW 目标",
                details={"target_table": desired.target_table},
            )

    async def _upsert_one_desired(self, desired: DesiredSyncTable) -> None:
        """在目标表 generation 串行锁内更新一项期望。"""
        desired_hash = desired.desired_hash()
        identity = self._identity_predicate(desired)
        changed_task_ids = select(data_sync_task.c.id).where(
            identity,
            data_sync_task.c.desired_hash != desired_hash,
        )
        # 步骤一：结构身份变化时删除旧 generation 的缓冲事件。
        await self._session.execute(
            delete(data_sync_event).where(
                data_sync_event.c.task_id.in_(changed_task_ids)
            )
        )
        # 步骤二：清空旧基线、游标和位点，从新 generation 完整回填。
        await self._session.execute(
            update(data_sync_task)
            .where(identity, data_sync_task.c.desired_hash != desired_hash)
            .values(
                desired_json=desired.model_dump(mode="json"),
                desired_hash=desired_hash,
                phase=SyncPhase.PENDING_SCHEMA.value,
                attempts=0,
                available_at=func.now(),
                lease_token=None,
                lease_expires_at=None,
                last_error_type=None,
                snapshot_file=None,
                snapshot_position=None,
                captured_file=None,
                captured_position=None,
                captured_row_index=None,
                applied_file=None,
                applied_position=None,
                applied_row_index=None,
                last_backfill_key=None,
                updated_at=func.now(),
            )
        )
        # 步骤三：插入首次出现的期望；重复快照保留当前阶段、租约和位点。
        statement = insert(data_sync_task).values(
            source=desired.source,
            source_schema=desired.source_schema,
            source_table=desired.source_table,
            target_table=desired.target_table,
            desired_json=desired.model_dump(mode="json"),
            desired_hash=desired_hash,
            phase=SyncPhase.PENDING_SCHEMA.value,
        )
        await self._session.execute(
            statement.on_duplicate_key_update(
                desired_json=func.if_(
                    data_sync_task.c.desired_hash == statement.inserted.desired_hash,
                    statement.inserted.desired_json,
                    data_sync_task.c.desired_json,
                )
            )
        )

    async def claim_tasks(
        self,
        *,
        limit: int,
        lease_seconds: int,
        max_attempts: int,
    ) -> list[ClaimedSyncTask]:
        """用数据库时钟领取一批可执行任务。"""
        # 步骤一：配置下调后，先把已耗尽新预算的任务收敛到死信。
        await self._session.execute(
            update(data_sync_task)
            .where(
                data_sync_task.c.phase.in_(_RUNNABLE_PHASES),
                data_sync_task.c.attempts >= max_attempts,
            )
            .values(
                phase=SyncPhase.DEAD.value,
                lease_token=None,
                lease_expires_at=None,
                last_error_type="retry_budget_exhausted",
                updated_at=func.now(),
            )
        )
        # 步骤二：锁定已到执行时间且租约为空或过期的有限任务。
        result = await self._session.execute(
            select(data_sync_task)
            .where(
                data_sync_task.c.phase.in_(_RUNNABLE_PHASES),
                data_sync_task.c.attempts < max_attempts,
                data_sync_task.c.available_at <= func.now(),
                or_(
                    data_sync_task.c.lease_token.is_(None),
                    data_sync_task.c.lease_expires_at <= func.now(),
                ),
            )
            .order_by(data_sync_task.c.available_at, data_sync_task.c.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = result.mappings().all()
        claimed: list[ClaimedSyncTask] = []
        # 步骤三：为每个任务生成独立令牌并在同一短事务内写入租约。
        for row in rows:
            lease_token = secrets.token_hex(16)
            await self._session.execute(
                update(data_sync_task)
                .where(data_sync_task.c.id == row["id"])
                .values(
                    lease_token=lease_token,
                    lease_expires_at=func.timestampadd(
                        text("SECOND"),
                        lease_seconds,
                        func.now(),
                    ),
                    updated_at=func.now(),
                )
            )
            claimed.append(self._claimed_task(row, lease_token))
        return claimed

    async def read_desired_tables(self) -> list[DesiredSyncTable]:
        """读取启动权限探测所需的全部当前源表契约。"""
        result = await self._session.execute(select(data_sync_task.c.desired_json))
        return [DesiredSyncTable.model_validate(item) for item in result.scalars()]

    async def read_readiness_phases(
        self,
        *,
        target_table: str,
        source: str | None,
    ) -> list[SyncPhase]:
        """只读查询一个回答依赖匹配到的全部同步阶段。"""
        # 步骤一：按稳定业务身份选择有限字段，不加锁、不领取租约也不推进状态。
        statement = select(data_sync_task.c.phase).where(
            data_sync_task.c.target_table == target_table
        )
        if source is not None:
            statement = statement.where(data_sync_task.c.source == source)
        result = await self._session.execute(statement.order_by(data_sync_task.c.id))
        return [SyncPhase(str(phase)) for phase in result.scalars()]

    async def renew_lease(
        self,
        task_id: int,
        lease_token: str,
        *,
        lease_seconds: int,
    ) -> bool:
        """仅由当前持有者续租任务。"""
        result = await self._session.execute(
            update(data_sync_task)
            .where(
                data_sync_task.c.id == task_id,
                data_sync_task.c.lease_token == lease_token,
                data_sync_task.c.lease_expires_at > func.now(),
            )
            .values(
                lease_expires_at=func.timestampadd(
                    text("SECOND"),
                    lease_seconds,
                    func.now(),
                ),
                updated_at=func.now(),
            )
        )
        return bool(_rowcount(result))

    async def has_authority(self, task: ClaimedSyncTask) -> bool:
        """使用数据库时钟确认 generation 与租约仍由当前 worker 持有。"""
        result = await self._session.execute(
            select(data_sync_task.c.id).where(self._task_authority(task)).limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def source_key_documents(
        self, *, target_table: str, source: str
    ) -> list[str]:
        """读取一个来源曾拥有的全部目标主键文档。"""
        result = await self._session.execute(
            select(data_sync_key_owner.c.primary_key_json).where(
                data_sync_key_owner.c.target_table == target_table,
                data_sync_key_owner.c.source == source,
            )
        )
        return [str(value) for value in result.scalars()]

    async def delete_source_key_owners(self, *, target_table: str, source: str) -> None:
        """在新基线建立前删除一个来源的旧主键归属。"""
        await self._session.execute(
            delete(data_sync_key_owner).where(
                data_sync_key_owner.c.target_table == target_table,
                data_sync_key_owner.c.source == source,
            )
        )

    async def settle_phase(
        self,
        task: ClaimedSyncTask,
        phase: SyncPhase,
        *,
        release_lease: bool = True,
        delay_seconds: float = 0,
    ) -> bool:
        """在期望身份和租约仍有效时推进任务阶段。"""
        values: dict[str, object] = {
            "phase": phase.value,
            "attempts": 0,
            "available_at": func.timestampadd(
                text("MICROSECOND"),
                max(0, int(delay_seconds * 1_000_000)),
                func.now(),
            ),
            "last_error_type": None,
            "updated_at": func.now(),
        }
        if release_lease:
            values.update(lease_token=None, lease_expires_at=None)
        result = await self._session.execute(
            update(data_sync_task).where(self._task_authority(task)).values(**values)
        )
        return bool(_rowcount(result))

    async def record_snapshot(
        self,
        task: ClaimedSyncTask,
        coordinate: BinlogCoordinate,
    ) -> bool:
        """记录回填基线对应的源 Binlog 位点。"""
        result = await self._session.execute(
            update(data_sync_task)
            .where(self._task_authority(task))
            .values(
                snapshot_file=coordinate.file,
                snapshot_position=coordinate.position,
                updated_at=func.now(),
            )
        )
        return bool(_rowcount(result))

    async def record_backfill_cursor(
        self,
        task: ClaimedSyncTask,
        primary_key: Sequence[object],
    ) -> bool:
        """在目标批次成功后保存最后完成的主键游标。"""
        result = await self._session.execute(
            update(data_sync_task)
            .where(self._task_authority(task))
            .values(
                last_backfill_key=[encode_row_value(value) for value in primary_key],
                updated_at=func.now(),
            )
        )
        return bool(_rowcount(result))

    async def advance_captured_coordinate(
        self,
        task: ClaimedSyncTask,
        coordinate: BinlogCoordinate,
    ) -> bool:
        """仅向前推进已持久化到事件缓冲区的 Binlog 位点。"""
        result = await self._session.execute(
            update(data_sync_task)
            .where(self._task_authority(task))
            .values(
                captured_file=coordinate.file,
                captured_position=coordinate.position,
                captured_row_index=coordinate.row_index,
                updated_at=func.now(),
            )
        )
        return bool(_rowcount(result))

    async def advance_applied_coordinate(
        self,
        task: ClaimedSyncTask,
        coordinate: BinlogCoordinate,
    ) -> bool:
        """仅向前推进已成功落入 DW 的 Binlog 位点。"""
        current_file_is_older = or_(
            func.char_length(data_sync_task.c.applied_file) < len(coordinate.file),
            and_(
                func.char_length(data_sync_task.c.applied_file) == len(coordinate.file),
                data_sync_task.c.applied_file < coordinate.file,
            ),
        )
        current_is_older = or_(
            data_sync_task.c.applied_file.is_(None),
            current_file_is_older,
            and_(
                data_sync_task.c.applied_file == coordinate.file,
                or_(
                    data_sync_task.c.applied_position < coordinate.position,
                    and_(
                        data_sync_task.c.applied_position == coordinate.position,
                        data_sync_task.c.applied_row_index < coordinate.row_index,
                    ),
                ),
            ),
        )
        result = await self._session.execute(
            update(data_sync_task)
            .where(self._task_authority(task), current_is_older)
            .values(
                applied_file=coordinate.file,
                applied_position=coordinate.position,
                applied_row_index=coordinate.row_index,
                updated_at=func.now(),
            )
        )
        return bool(_rowcount(result))

    async def retry_failure(
        self,
        task: ClaimedSyncTask,
        *,
        error_type: str,
        retry_base_seconds: int,
        retry_max_seconds: int,
        max_attempts: int,
    ) -> SyncPhase | None:
        """记录一次可重试失败，并在预算耗尽时转入死信。"""
        # 步骤一：锁定仍由当前 worker 持有的期望版本，防止迟到失败覆盖新任务。
        result = await self._session.execute(
            select(data_sync_task.c.attempts)
            .where(self._task_authority(task))
            .with_for_update()
        )
        attempts = result.scalar_one_or_none()
        if attempts is None:
            return None
        next_attempt = attempts + 1
        phase = (
            SyncPhase.DEAD
            if next_attempt >= max_attempts
            else (
                SyncPhase.REPLAYING
                if task.phase is SyncPhase.STREAMING
                else task.phase
            )
        )
        delay = min(
            retry_base_seconds * (2 ** min(attempts, 20)),
            retry_max_seconds,
        )
        # 步骤二：释放租约；死信保留安全错误类型，普通失败按数据库时钟退避。
        await self._session.execute(
            update(data_sync_task)
            .where(self._task_authority(task))
            .values(
                phase=phase.value,
                attempts=next_attempt,
                available_at=func.timestampadd(text("SECOND"), delay, func.now()),
                lease_token=None,
                lease_expires_at=None,
                last_error_type=error_type[:128],
                updated_at=func.now(),
            )
        )
        return phase

    async def hold_failure(
        self,
        task: ClaimedSyncTask,
        *,
        phase: SyncPhase,
        error_type: str,
    ) -> bool:
        """把确定性失败保留为暂停或冲突状态。"""
        if phase not in (SyncPhase.PAUSED, SyncPhase.CONFLICT):
            raise ValueError("确定性失败只能进入 paused 或 conflict")
        result = await self._session.execute(
            update(data_sync_task)
            .where(self._task_authority(task))
            .values(
                phase=phase.value,
                lease_token=None,
                lease_expires_at=None,
                last_error_type=error_type[:128],
                updated_at=func.now(),
            )
        )
        return bool(_rowcount(result))

    async def append_event(self, task_id: int, event: SyncRowEvent) -> bool:
        """按稳定 Binlog 行坐标幂等暂存事件。"""
        statement = insert(data_sync_event).values(
            task_id=task_id,
            source=event.source,
            binlog_file=event.coordinate.file,
            binlog_position=event.coordinate.position,
            row_index=event.coordinate.row_index,
            payload_json=event.model_dump(mode="json"),
        )
        result = await self._session.execute(statement.prefix_with("IGNORE"))
        return bool(_rowcount(result))

    async def read_events(
        self,
        task_id: int,
        *,
        limit: int,
    ) -> list[BufferedSyncEvent]:
        """按 Binlog 顺序读取有限数量的未确认事件。"""
        result = await self._session.execute(
            select(data_sync_event.c.id, data_sync_event.c.payload_json)
            .where(
                data_sync_event.c.task_id == task_id,
                data_sync_event.c.acknowledged_at.is_(None),
            )
            .order_by(
                func.char_length(data_sync_event.c.binlog_file),
                data_sync_event.c.binlog_file,
                data_sync_event.c.binlog_position,
                data_sync_event.c.row_index,
            )
            .limit(limit)
        )
        return [
            BufferedSyncEvent(
                id=row.id,
                event=SyncRowEvent.model_validate(row.payload_json),
            )
            for row in result
        ]

    async def count_pending_events(self, task_id: int) -> int:
        """返回单个任务尚未确认的暂存事件数量。"""
        result = await self._session.execute(
            select(func.count())
            .select_from(data_sync_event)
            .where(
                data_sync_event.c.task_id == task_id,
                data_sync_event.c.acknowledged_at.is_(None),
            )
        )
        return int(result.scalar_one())

    async def acknowledge_event(self, task_id: int, event_id: int) -> bool:
        """标记一个已在同事务中成功应用到 DW 的暂存事件。"""
        result = await self._session.execute(
            update(data_sync_event)
            .where(
                data_sync_event.c.id == event_id,
                data_sync_event.c.task_id == task_id,
                data_sync_event.c.acknowledged_at.is_(None),
            )
            .values(acknowledged_at=func.now())
        )
        return bool(_rowcount(result))

    async def cleanup_events(self, task_id: int, *, limit: int) -> int:
        """分批删除已确认的暂存事件。"""
        ids_result = await self._session.execute(
            select(data_sync_event.c.id)
            .where(
                data_sync_event.c.task_id == task_id,
                data_sync_event.c.acknowledged_at.is_not(None),
            )
            .order_by(data_sync_event.c.id)
            .limit(max(0, limit))
        )
        ids = list(ids_result.scalars())
        if not ids:
            return 0
        result = await self._session.execute(
            delete(data_sync_event).where(data_sync_event.c.id.in_(ids))
        )
        return _rowcount(result)

    async def claim_key_owner(
        self,
        *,
        target_table: str,
        primary_key_hash: str,
        primary_key_json: str,
        source: str,
    ) -> KeyConflict | None:
        """领取目标主键归属，并复核哈希对应的完整主键文档。"""
        # 步骤一：先以唯一键幂等建立首次归属，避免并发 SELECT 后同时 INSERT。
        await self._session.execute(
            insert(data_sync_key_owner)
            .values(
                target_table=target_table,
                primary_key_hash=primary_key_hash,
                primary_key_json=primary_key_json,
                source=source,
                deleted=False,
            )
            .prefix_with("IGNORE")
        )
        # 步骤二：锁定唯一归属并复核哈希对应的完整主键文档。
        result = await self._session.execute(
            select(data_sync_key_owner)
            .where(
                data_sync_key_owner.c.target_table == target_table,
                data_sync_key_owner.c.primary_key_hash == primary_key_hash,
            )
            .with_for_update()
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise RuntimeError("目标主键归属写入后无法读取")
        if row["primary_key_json"] == primary_key_json and row["source"] == source:
            await self._session.execute(
                update(data_sync_key_owner)
                .where(
                    data_sync_key_owner.c.target_table == target_table,
                    data_sync_key_owner.c.primary_key_hash == primary_key_hash,
                )
                .values(deleted=False, updated_at=func.now())
            )
            return None
        return KeyConflict(
            target_table=target_table,
            primary_key_hash=primary_key_hash,
            owner_source=row["source"],
            contender_source=source,
        )

    async def tombstone_key_owner(
        self,
        *,
        target_table: str,
        primary_key_hash: str,
        source: str,
    ) -> bool:
        """源行删除后保留归属墓碑，禁止其他来源复用主键。"""
        result = await self._session.execute(
            update(data_sync_key_owner)
            .where(
                data_sync_key_owner.c.target_table == target_table,
                data_sync_key_owner.c.primary_key_hash == primary_key_hash,
                data_sync_key_owner.c.source == source,
            )
            .values(deleted=True, updated_at=func.now())
        )
        return bool(_rowcount(result))

    @staticmethod
    def _identity_predicate(desired: DesiredSyncTable) -> Any:
        """构建同步任务业务唯一键条件。"""
        return and_(
            data_sync_task.c.source == desired.source,
            data_sync_task.c.source_schema == desired.source_schema,
            data_sync_task.c.source_table == desired.source_table,
            data_sync_task.c.target_table == desired.target_table,
        )

    @staticmethod
    def _task_authority(task: ClaimedSyncTask) -> Any:
        """构建期望版本与租约的 compare-and-set 条件。"""
        return and_(
            data_sync_task.c.id == task.id,
            data_sync_task.c.desired_hash == task.desired_hash,
            data_sync_task.c.lease_token == task.lease_token,
            data_sync_task.c.lease_expires_at > func.now(),
        )

    @staticmethod
    def _claimed_task(
        row: RowMapping,
        lease_token: str,
    ) -> ClaimedSyncTask:
        """把锁定行转换为 worker 使用的有界任务投影。"""
        snapshot = None
        if row["snapshot_file"] is not None and row["snapshot_position"] is not None:
            snapshot = BinlogCoordinate(
                file=str(row["snapshot_file"]),
                position=int(row["snapshot_position"]),
                row_index=0,
            )
        applied = None
        if (
            row["applied_file"] is not None
            and row["applied_position"] is not None
            and row["applied_row_index"] is not None
        ):
            applied = BinlogCoordinate(
                file=str(row["applied_file"]),
                position=int(row["applied_position"]),
                row_index=int(row["applied_row_index"]),
            )
        captured = None
        if (
            row["captured_file"] is not None
            and row["captured_position"] is not None
            and row["captured_row_index"] is not None
        ):
            captured = BinlogCoordinate(
                file=str(row["captured_file"]),
                position=int(row["captured_position"]),
                row_index=int(row["captured_row_index"]),
            )
        raw_cursor = row["last_backfill_key"]
        return ClaimedSyncTask(
            id=int(row["id"]),
            desired=DesiredSyncTable.model_validate(row["desired_json"]),
            desired_hash=str(row["desired_hash"]),
            phase=SyncPhase(str(row["phase"])),
            lease_token=lease_token,
            attempts=int(row["attempts"]),
            snapshot=snapshot,
            captured=captured,
            applied=applied,
            last_backfill_key=(
                tuple(decode_row_value(value) for value in raw_cursor)
                if isinstance(raw_cursor, (list, tuple))
                else None
            ),
        )
