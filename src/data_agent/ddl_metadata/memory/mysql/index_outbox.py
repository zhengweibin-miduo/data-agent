"""记忆派生索引期望状态仓储。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import RowMapping, delete, func, select, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.ddl_metadata.memory.domain.payloads import content_object_ids
from data_agent.ddl_metadata.memory.mysql.tables import (
    agent_memory,
    memory_index_outbox,
)
from data_agent.ddl_metadata.models.memory import (
    MEMORY_CONTENT_ADAPTER,
    MemoryIndexOperation,
    MemoryIndexTarget,
    MemoryKind,
    MemoryOutboxItem,
    MemoryProjection,
    MemoryStatus,
    MemoryTrust,
)
from data_agent.settings import app_config


class MemoryIndexOutboxRepository:
    """在调用方事务中管理派生索引期望状态与投影读取。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定由调用方管理事务边界的 Session。"""
        self._session = session

    async def set_desired_state(
        self,
        memory_uids: set[str],
        operation: MemoryIndexOperation,
    ) -> None:
        """覆盖每个索引目标的期望状态。"""
        if not memory_uids:
            return
        values = [
            {
                "memory_uid": uid,
                "target": target.value,
                "operation": operation.value,
                "projection_version": app_config.memory.projection_version,
                "attempts": 0,
                "available_at": func.now(),
                "last_error_type": None,
            }
            for uid in memory_uids
            for target in MemoryIndexTarget
        ]
        statement = insert(memory_index_outbox).values(values)
        await self._session.execute(
            statement.on_duplicate_key_update(
                operation=statement.inserted.operation,
                projection_version=statement.inserted.projection_version,
                attempts=0,
                available_at=func.now(),
                last_error_type=None,
            )
        )

    async def pending_targets(
        self,
        uids: set[str],
    ) -> dict[str, set[MemoryIndexTarget]]:
        """批量读取尚未确认的派生索引目标。"""
        if not uids:
            return {}
        rows = (
            await self._session.execute(
                select(
                    memory_index_outbox.c.memory_uid,
                    memory_index_outbox.c.target,
                ).where(memory_index_outbox.c.memory_uid.in_(uids))
            )
        ).all()
        result: dict[str, set[MemoryIndexTarget]] = {}
        for uid, target in rows:
            result.setdefault(str(uid), set()).add(MemoryIndexTarget(str(target)))
        return result

    async def claim_outbox(self, limit: int) -> list[MemoryOutboxItem]:
        """通过行锁有界领取可执行索引期望状态。"""
        rows = (
            await self._session.execute(
                select(memory_index_outbox)
                .where(memory_index_outbox.c.available_at <= func.now())
                .order_by(memory_index_outbox.c.updated_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).mappings()
        return [
            MemoryOutboxItem(
                memory_uid=str(row["memory_uid"]),
                target=MemoryIndexTarget(str(row["target"])),
                operation=MemoryIndexOperation(str(row["operation"])),
                projection_version=str(row["projection_version"]),
                attempts=int(row["attempts"]),
            )
            for row in rows
        ]

    async def acknowledge_outbox(self, item: MemoryOutboxItem) -> None:
        """仅确认仍与已处理期望状态相同的 outbox 行。"""
        await self._session.execute(
            delete(memory_index_outbox).where(
                memory_index_outbox.c.memory_uid == item.memory_uid,
                memory_index_outbox.c.target == item.target.value,
                memory_index_outbox.c.operation == item.operation.value,
                memory_index_outbox.c.projection_version == item.projection_version,
            )
        )

    async def retry_outbox(
        self,
        item: MemoryOutboxItem,
        error_type: str,
        max_backoff_seconds: int,
    ) -> None:
        """记录安全异常类型并指数退避。"""
        attempts = item.attempts + 1
        delay = min(2 ** min(attempts, 20), max_backoff_seconds)
        available_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=delay)
        await self._session.execute(
            update(memory_index_outbox)
            .where(
                memory_index_outbox.c.memory_uid == item.memory_uid,
                memory_index_outbox.c.target == item.target.value,
            )
            .values(
                attempts=attempts,
                available_at=available_at,
                last_error_type=error_type[:128],
            )
        )

    async def projection(self, uid: str) -> MemoryProjection | None:
        """从权威内容构造共享索引投影。"""
        row = (
            (
                await self._session.execute(
                    select(agent_memory).where(agent_memory.c.uid == uid)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        content = MEMORY_CONTENT_ADAPTER.validate_python(row["content"])
        return MemoryProjection(
            memory_uid=str(row["uid"]),
            source=str(row["source"]),
            kind=MemoryKind(str(row["kind"])),
            scope_key=str(row["scope_key"]),
            schema_fingerprint=str(row["schema_fingerprint"]),
            memory_text=str(row["memory_text"]),
            content_hash=str(row["content_hash"]),
            object_ids=content_object_ids(content),
            trust=MemoryTrust(str(row["trust"])),
            status=MemoryStatus(str(row["status"])),
            content_version=str(row["content_version"]),
            projection_version=str(row["projection_version"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def scan_active(
        self,
        *,
        after_id: int,
        limit: int,
    ) -> list[RowMapping]:
        """按 MySQL 主键游标扫描活动记忆。"""
        return list(
            (
                await self._session.execute(
                    select(agent_memory.c.id, agent_memory.c.uid)
                    .where(
                        agent_memory.c.id > after_id,
                        agent_memory.c.status == MemoryStatus.ACTIVE.value,
                    )
                    .order_by(agent_memory.c.id)
                    .limit(limit)
                )
            ).mappings()
        )

    async def enqueue_rebuild(self, uids: set[str]) -> None:
        """为活动 UID 重新生成双目标 UPSERT 期望状态。"""
        await self.set_desired_state(uids, MemoryIndexOperation.UPSERT)
