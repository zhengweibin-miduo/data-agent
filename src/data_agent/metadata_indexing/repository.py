"""Meta 索引 desired state 的短事务仓储。"""

import hashlib
import json
from uuid import uuid4

from sqlalchemy import and_, case, delete, func, or_, select, text, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from data_agent.metadata_indexing.models import (
    ClaimedMetadataIndexWork,
    MetadataIndexDesired,
    MetadataIndexOperation,
    MetadataIndexTarget,
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
        rows = [item.model_dump(mode="json") for item in desired]
        statement = insert(metadata_index_outbox).values(rows)
        changed = (
            metadata_index_outbox.c.desired_version
            != statement.inserted.desired_version
        )
        continuing_refresh = and_(
            changed,
            metadata_index_outbox.c.target == MetadataIndexTarget.VALUES.value,
            metadata_index_outbox.c.operation == MetadataIndexOperation.REFRESH.value,
            or_(
                metadata_index_outbox.c.lease_token.is_not(None),
                metadata_index_outbox.c.progress_column_id.is_not(None),
            ),
        )
        replace_current = and_(changed, ~continuing_refresh)
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
                        case(
                            (continuing_refresh, statement.inserted.desired_version),
                            (replace_current, None),
                            else_=metadata_index_outbox.c.pending_desired_version,
                        ),
                    ),
                    # MySQL 从左到右计算赋值；版本列必须最后覆盖，前面的
                    # changed 表达式才能与行内旧版本比较。
                    (
                        "desired_version",
                        case(
                            (
                                continuing_refresh,
                                metadata_index_outbox.c.desired_version,
                            ),
                            else_=statement.inserted.desired_version,
                        ),
                    ),
                ]
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
