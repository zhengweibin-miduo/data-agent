"""Meta 索引 desired state 的短事务仓储。"""

import hashlib
import json
from uuid import uuid4

from sqlalchemy import case, delete, func, or_, select, text, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from data_agent.metadata_indexing.models import (
    ClaimedMetadataIndexWork,
    MetadataIndexDesired,
    MetadataIndexOperation,
    MetadataIndexTarget,
    MetadataValueRefreshPhase,
)
from data_agent.metadata_indexing.tables import metadata_index_outbox
from data_agent.settings import app_config


def metadata_desired_version(payload: object) -> str:
    """为规范化 desired payload 生成稳定版本。"""
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


class MetadataIndexOutboxRepository:
    """在调用方事务中合并、领取并结算 Meta 索引任务。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定调用方拥有的 Session。"""
        self._session = session

    async def enqueue(
        self,
        desired: list[MetadataIndexDesired],
        *,
        debounce_seconds: int = 0,
    ) -> None:
        """按目标与对象身份合并最新期望状态。"""
        if not desired:
            return
        semantic = [
            item for item in desired if item.target == MetadataIndexTarget.SEMANTIC
        ]
        values = [item for item in desired if item.target == MetadataIndexTarget.VALUES]
        if semantic:
            await self._enqueue_semantic(semantic, debounce_seconds=debounce_seconds)
        for item in values:
            await self._enqueue_value(item, debounce_seconds=debounce_seconds)

    async def _enqueue_semantic(
        self,
        desired: list[MetadataIndexDesired],
        *,
        debounce_seconds: int,
    ) -> None:
        """批量合并原有语义索引期望状态。"""
        rows = [item.model_dump(mode="json") for item in desired]
        statement = insert(metadata_index_outbox).values(rows)
        changed = (
            metadata_index_outbox.c.desired_version
            != statement.inserted.desired_version
        )
        replace_current = changed
        available = func.timestampadd(text("SECOND"), debounce_seconds, func.now())
        await self._session.execute(
            statement.on_duplicate_key_update(
                [
                    ("operation", statement.inserted.operation),
                    (
                        "attempts",
                        case(
                            (replace_current, 0),
                            else_=metadata_index_outbox.c.attempts,
                        ),
                    ),
                    (
                        "available_at",
                        case(
                            # 保留首次变更建立的最早执行期限；持续到达的新版本
                            # 只能提前而不能反复推迟同一对象的刷新。
                            (
                                replace_current,
                                func.least(
                                    metadata_index_outbox.c.available_at,
                                    available,
                                ),
                            ),
                            else_=metadata_index_outbox.c.available_at,
                        ),
                    ),
                    (
                        "lease_token",
                        case(
                            (replace_current, None),
                            else_=metadata_index_outbox.c.lease_token,
                        ),
                    ),
                    (
                        "lease_expires_at",
                        case(
                            (replace_current, None),
                            else_=metadata_index_outbox.c.lease_expires_at,
                        ),
                    ),
                    (
                        "last_error_type",
                        case(
                            (replace_current, None),
                            else_=metadata_index_outbox.c.last_error_type,
                        ),
                    ),
                    (
                        "progress_column_id",
                        case(
                            (replace_current, None),
                            else_=metadata_index_outbox.c.progress_column_id,
                        ),
                    ),
                    (
                        "pending_desired_version",
                        None,
                    ),
                    # MySQL 从左到右计算赋值；版本列必须最后覆盖，前面的
                    # changed 表达式才能与行内旧版本比较。
                    (
                        "desired_version",
                        statement.inserted.desired_version,
                    ),
                ]
            )
        )

    async def _enqueue_value(
        self,
        item: MetadataIndexDesired,
        *,
        debounce_seconds: int,
    ) -> None:
        """锁定单个 VALUES 状态并合并活跃版本。"""
        frequency_version = item.frequency_version or item.desired_version
        identity = (
            metadata_index_outbox.c.target == item.target.value,
            metadata_index_outbox.c.object_kind == item.object_kind.value,
            metadata_index_outbox.c.object_id == item.object_id,
        )
        current = (
            (
                await self._session.execute(
                    select(metadata_index_outbox)
                    .where(*identity)
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )
        available = func.timestampadd(text("SECOND"), debounce_seconds, func.now())
        if current is None:
            row = item.model_dump(mode="json")
            row["frequency_version"] = frequency_version
            row["phase"] = MetadataValueRefreshPhase.SCAN.value
            row["index_generation"] = frequency_version
            row["available_at"] = available
            await self._session.execute(
                insert(metadata_index_outbox).values(**row)
            )
            return
        if current["desired_version"] == item.desired_version:
            return
        active = (
            current["lease_token"] is not None
            or current["phase"] != MetadataValueRefreshPhase.COMPLETE.value
        )
        if active:
            await self._session.execute(
                update(metadata_index_outbox)
                .where(*identity)
                .values(
                    pending_desired_version=item.desired_version,
                    pending_frequency_version=frequency_version,
                    available_at=func.least(
                        metadata_index_outbox.c.available_at,
                        available,
                    ),
                )
            )
            return
        same_frequency = current["frequency_version"] == frequency_version
        await self._session.execute(
            update(metadata_index_outbox)
            .where(*identity)
            .values(
                operation=item.operation.value,
                desired_version=item.desired_version,
                pending_desired_version=None,
                frequency_version=frequency_version,
                pending_frequency_version=None,
                phase=(
                    MetadataValueRefreshPhase.SELECT_TOP_N.value
                    if same_frequency
                    else MetadataValueRefreshPhase.SCAN.value
                ),
                progress_column_id=None,
                last_primary_key=None,
                bulk_cursor=None,
                # 普通刷新必须沿用 publication generation，才能把上一频次代次
                # 已发布但不再属于 Top-N 的文档纳入本轮 cleanup。
                index_generation=current["index_generation"],
                attempts=0,
                available_at=available,
                lease_token=None,
                lease_expires_at=None,
                last_error_type=None,
            )
        )

    async def claim(self, limit: int | None = None) -> list[ClaimedMetadataIndexWork]:
        """短锁领取当前可执行任务并写入数据库时钟租约。"""
        batch_size = limit or app_config.metadata_index.dispatch_batch_size
        rows = (
            (
                await self._session.execute(
                    select(metadata_index_outbox)
                    .where(
                        metadata_index_outbox.c.attempts
                        < app_config.metadata_index.max_attempts,
                    metadata_index_outbox.c.available_at <= func.now(),
                    or_(
                        metadata_index_outbox.c.target
                        != MetadataIndexTarget.VALUES.value,
                        metadata_index_outbox.c.phase
                        != MetadataValueRefreshPhase.COMPLETE.value,
                    ),
                        or_(
                            metadata_index_outbox.c.lease_expires_at.is_(None),
                            metadata_index_outbox.c.lease_expires_at <= func.now(),
                        ),
                    )
                    .order_by(metadata_index_outbox.c.available_at)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            )
            .mappings()
            .all()
        )
        claimed: list[ClaimedMetadataIndexWork] = []
        for row in rows:
            token = uuid4().hex
            await self._session.execute(
                update(metadata_index_outbox)
                .where(
                    metadata_index_outbox.c.target == row["target"],
                    metadata_index_outbox.c.object_kind == row["object_kind"],
                    metadata_index_outbox.c.object_id == row["object_id"],
                    metadata_index_outbox.c.desired_version == row["desired_version"],
                )
                .values(
                    lease_token=token,
                    lease_expires_at=func.timestampadd(
                        text("SECOND"),
                        app_config.metadata_index.claim_lease_seconds,
                        func.now(),
                    ),
                )
            )
            claimed.append(
                ClaimedMetadataIndexWork(
                    target=row["target"],
                    object_kind=row["object_kind"],
                    object_id=row["object_id"],
                    operation=row["operation"],
                    desired_version=row["desired_version"],
                    lease_token=token,
                    progress_column_id=row["progress_column_id"],
                    frequency_version=row["frequency_version"],
                    phase=row["phase"],
                    last_primary_key=row["last_primary_key"],
                    bulk_cursor=row["bulk_cursor"],
                    index_generation=row["index_generation"],
                )
            )
        return claimed

    async def is_authoritative(self, item: ClaimedMetadataIndexWork) -> bool:
        """在外部修改前确认领取身份仍是当前权威期望状态。"""
        return bool(
            await self._session.scalar(
                select(func.count())
                .select_from(metadata_index_outbox)
                .where(*self._authority(item))
            )
        )

    async def renew_lease(self, item: ClaimedMetadataIndexWork) -> bool:
        """仅为仍未过期且身份完整匹配的领取续租。"""
        result = await self._session.execute(
            update(metadata_index_outbox)
            .where(
                *self._authority(item),
                metadata_index_outbox.c.lease_expires_at > func.now(),
            )
            .values(
                lease_expires_at=func.timestampadd(
                    text("SECOND"),
                    app_config.metadata_index.claim_lease_seconds,
                    func.now(),
                )
            )
        )
        return isinstance(result, CursorResult) and bool(result.rowcount)

    def _authority(
        self,
        item: ClaimedMetadataIndexWork,
    ) -> tuple[ColumnElement[bool], ...]:
        """构造结算必须匹配的完整 desired-state 身份。"""
        return (
            metadata_index_outbox.c.target == item.target.value,
            metadata_index_outbox.c.object_kind == item.object_kind.value,
            metadata_index_outbox.c.object_id == item.object_id,
            metadata_index_outbox.c.operation == item.operation.value,
            metadata_index_outbox.c.desired_version == item.desired_version,
            metadata_index_outbox.c.lease_token == item.lease_token,
        )

    async def acknowledge(self, item: ClaimedMetadataIndexWork) -> bool:
        """仅确认仍由当前 worker 持有的完整期望状态。"""
        promoted = await self._session.execute(
            update(metadata_index_outbox)
            .where(
                *self._authority(item),
                metadata_index_outbox.c.pending_desired_version.is_not(None),
            )
            .values(
                desired_version=metadata_index_outbox.c.pending_desired_version,
                pending_desired_version=None,
                progress_column_id=None,
                attempts=0,
                available_at=func.now(),
                lease_token=None,
                lease_expires_at=None,
                last_error_type=None,
            )
        )
        if isinstance(promoted, CursorResult) and bool(promoted.rowcount):
            return True
        result = await self._session.execute(
            delete(metadata_index_outbox).where(*self._authority(item))
        )
        return isinstance(result, CursorResult) and bool(result.rowcount)

    async def advance_progress(
        self,
        item: ClaimedMetadataIndexWork,
        column_id: str,
    ) -> bool:
        """按完整领取身份保存已完成字段并立即释放租约。"""
        result = await self._session.execute(
            update(metadata_index_outbox)
            .where(*self._authority(item))
            .values(
                progress_column_id=column_id,
                available_at=func.now(),
                lease_token=None,
                lease_expires_at=None,
                last_error_type=None,
                attempts=0,
            )
        )
        return isinstance(result, CursorResult) and bool(result.rowcount)

    async def advance_value_state(
        self,
        item: ClaimedMetadataIndexWork,
        *,
        phase: MetadataValueRefreshPhase,
        progress_column_id: str | None = None,
        last_primary_key: dict[str, object] | None = None,
        bulk_cursor: dict[str, object] | None = None,
    ) -> bool:
        """推进一个 VALUES 工作单元，或在边界提升最新 pending 版本。"""
        row = (
            (
                await self._session.execute(
                    select(metadata_index_outbox)
                    .where(*self._authority(item))
                    .with_for_update()
                )
            )
            .mappings()
            .one_or_none()
        )

        if row is None:
            return False
        values: dict[str, object] = {
            "available_at": func.now(),
            "lease_token": None,
            "lease_expires_at": None,
            "last_error_type": None,
            "attempts": 0,
        }
        pending = row["pending_desired_version"]
        if pending is not None:
            pending_frequency = row["pending_frequency_version"]
            same_frequency = pending_frequency == row["frequency_version"]
            values.update(
                desired_version=pending,
                pending_desired_version=None,
                frequency_version=pending_frequency,
                pending_frequency_version=None,
                bulk_cursor=None,
            )
            if same_frequency and row["phase"] == MetadataValueRefreshPhase.SCAN.value:
                values.update(
                    phase=MetadataValueRefreshPhase.SCAN.value,
                    progress_column_id=row["progress_column_id"],
                    last_primary_key=row["last_primary_key"],
                )
            else:
                values.update(
                    phase=(
                        MetadataValueRefreshPhase.SELECT_TOP_N.value
                        if same_frequency
                        else MetadataValueRefreshPhase.SCAN.value
                    ),
                    progress_column_id=None,
                    last_primary_key=None,
                    # pending 代次仍属于同一次物理索引生命周期；沿用已发布
                    # 集合的 generation，避免旧成员逃逸清理。
                    index_generation=row["index_generation"],
                )
        else:
            values.update(
                phase=phase.value,
                progress_column_id=progress_column_id,
                last_primary_key=last_primary_key,
                bulk_cursor=bulk_cursor,
            )
        result = await self._session.execute(
            update(metadata_index_outbox)
            .where(*self._authority(item))
            .values(**values)
        )
        return isinstance(result, CursorResult) and bool(result.rowcount)

    async def lock_authoritative(self, item: ClaimedMetadataIndexWork) -> bool:
        """锁定仍由当前 worker 持有的 VALUES 状态行。"""
        return (
            await self._session.scalar(
                select(metadata_index_outbox.c.object_id)
                .where(*self._authority(item))
                .with_for_update()
            )
            is not None
        )

    async def restore_reconciliation(self, item: ClaimedMetadataIndexWork) -> bool:
        """迟到写入无法确认时强制发布一次新的当前状态收敛。"""
        operation = (
            MetadataIndexOperation.UPSERT
            if item.target == MetadataIndexTarget.SEMANTIC
            else MetadataIndexOperation.REFRESH
        )
        repair_version = metadata_desired_version(
            {
                "repair": uuid4().hex,
                "target": item.target.value,
                "object_kind": item.object_kind.value,
                "object_id": item.object_id,
            }
        )
        statement = insert(metadata_index_outbox).values(
            target=item.target.value,
            object_kind=item.object_kind.value,
            object_id=item.object_id,
            operation=operation.value,
            desired_version=repair_version,
            attempts=0,
            available_at=func.now(),
            lease_token=None,
            lease_expires_at=None,
            last_error_type=None,
        )
        # 已存在的新期望状态本身就是收敛依据，迟到 worker 不得覆盖它；只有
        # outbox 已被并发确认删除时才插入新的 repair generation。
        statement = statement.on_duplicate_key_update(
            desired_version=metadata_index_outbox.c.desired_version
        )
        result = await self._session.execute(statement)
        return isinstance(result, CursorResult) and bool(result.rowcount)

    async def backoff(self, item: ClaimedMetadataIndexWork, error_type: str) -> bool:
        """仅为仍有权结算的远程失败增加有界退避。"""
        if item.target == MetadataIndexTarget.VALUES:
            row = (
                (
                    await self._session.execute(
                        select(metadata_index_outbox)
                        .where(*self._authority(item))
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                return False
            if (
                row["pending_desired_version"] is not None
                and int(row["attempts"]) + 1
                >= app_config.metadata_index.max_attempts
            ):
                return await self.advance_value_state(
                    item,
                    phase=item.phase or MetadataValueRefreshPhase.SCAN,
                    progress_column_id=item.progress_column_id,
                    last_primary_key=item.last_primary_key,
                    bulk_cursor=item.bulk_cursor,
                )
        else:
            promoted = await self._session.execute(
                update(metadata_index_outbox)
                .where(
                    *self._authority(item),
                    metadata_index_outbox.c.pending_desired_version.is_not(None),
                    metadata_index_outbox.c.attempts + 1
                    >= app_config.metadata_index.max_attempts,
                )
                .values(
                    desired_version=metadata_index_outbox.c.pending_desired_version,
                    pending_desired_version=None,
                    progress_column_id=None,
                    attempts=0,
                    available_at=func.now(),
                    lease_token=None,
                    lease_expires_at=None,
                    last_error_type=None,
                )
            )
            if isinstance(promoted, CursorResult) and bool(promoted.rowcount):
                return True
        seconds = func.least(
            func.pow(2, metadata_index_outbox.c.attempts),
            app_config.metadata_index.retry_max_seconds,
        )
        result = await self._session.execute(
            update(metadata_index_outbox)
            .where(*self._authority(item))
            .values(
                attempts=metadata_index_outbox.c.attempts + 1,
                available_at=func.timestampadd(text("SECOND"), seconds, func.now()),
                lease_token=None,
                lease_expires_at=None,
                last_error_type=error_type[:128],
            )
        )
        return isinstance(result, CursorResult) and bool(result.rowcount)

    async def defer(self, item: ClaimedMetadataIndexWork, seconds: int = 30) -> bool:
        """无损释放尚未物化投影的租约，不消耗失败预算。"""
        result = await self._session.execute(
            update(metadata_index_outbox)
            .where(*self._authority(item))
            .values(
                available_at=func.timestampadd(text("SECOND"), seconds, func.now()),
                lease_token=None,
                lease_expires_at=None,
                last_error_type=None,
            )
        )
        return isinstance(result, CursorResult) and bool(result.rowcount)

    async def pending_value_tables(self, table_ids: set[str]) -> set[str]:
        """读取仍存在值刷新期望状态的表标识。"""
        if not table_ids:
            return set()
        return set(
            (
                await self._session.scalars(
                    select(metadata_index_outbox.c.object_id).where(
                        metadata_index_outbox.c.target
                        == MetadataIndexTarget.VALUES.value,
                        metadata_index_outbox.c.object_id.in_(table_ids),
                        metadata_index_outbox.c.phase
                        != MetadataValueRefreshPhase.COMPLETE.value,
                    )
                )
            ).all()
        )

    async def dead_letter_count(self) -> int:
        """返回达到重试上限且仍遮蔽完整性的任务数。"""
        return int(
            await self._session.scalar(
                select(func.count())
                .select_from(metadata_index_outbox)
                .where(
                    metadata_index_outbox.c.attempts
                    >= app_config.metadata_index.max_attempts
                )
            )
            or 0
        )
