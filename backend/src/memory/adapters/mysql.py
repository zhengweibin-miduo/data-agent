"""Long-term Memory application ports 的 MySQL 生产适配器。"""

from __future__ import annotations

from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from errors import DataAgentError
from infrastructure.mysql import MySQLDatabase
from memory.application.contracts import (
    PreparedMemoryProjection,
    StoredMemory,
)
from memory.mysql.index_outbox import MemoryIndexOutboxRepository
from memory.mysql.repository import MemoryRepository
from models.memory import (
    MemoryCandidate,
    MemoryContent,
    MemoryDecision,
    MemoryDetail,
    MemoryHistoryPage,
    MemoryIndexTarget,
    MemoryOutboxItem,
    MemoryStatus,
)


class MemoryReferenceValidator(Protocol):
    """在 DDL 记忆提交事务内验证外部领域引用。"""

    async def validate(self, session: AsyncSession, content: MemoryContent) -> None:
        """验证记忆内容引用的外部对象。"""
        ...


class MySQLMemoryStore:
    """以每个用例一个托管短事务实现权威记忆操作。"""

    def __init__(self, references: MemoryReferenceValidator) -> None:
        """绑定事务内 DDL 引用校验器。"""
        self._references = references

    async def get(self, uid: str, *, user_id: str | None) -> StoredMemory | None:
        """按租户边界读取权威记忆。"""
        async with MySQLDatabase.session() as session:
            memory = await MemoryRepository(session).get_by_uid(uid, user_id=user_id)
        if memory is None:
            return None
        return StoredMemory(memory.id, memory.detail)

    async def history(
        self,
        uid: str,
        *,
        user_id: str | None,
        offset: int,
        limit: int,
    ) -> MemoryHistoryPage | None:
        """读取有界只追加历史。"""
        async with MySQLDatabase.session() as session:
            return await MemoryRepository(session).history(
                uid, user_id=user_id, offset=offset, limit=limit
            )

    async def replace(
        self,
        current_uid: str,
        candidate: MemoryCandidate,
        content: MemoryContent,
        *,
        user_id: str | None,
        expected_version: int,
    ) -> int:
        """锁定当前权威版本并原子替换、写历史与投影期望状态。"""
        async with MySQLDatabase.session() as session:
            repository = MemoryRepository(session)
            current = await repository.get_by_uid(
                current_uid, user_id=user_id, for_update=True
            )
            if (
                current is None
                or current.detail.status != MemoryStatus.ACTIVE
                or current.detail.record_version != expected_version
            ):
                raise DataAgentError(
                    "stale_memory",
                    "memory_update",
                    "目标记忆已发生变化",
                    http_status=409,
                )
            if user_id is None:
                await self._references.validate(session, content)
            await repository.upsert_candidates([candidate])
            if candidate.decision == MemoryDecision.NOOP:
                raise DataAgentError(
                    "stale_memory",
                    "memory_update",
                    "修正内容与已删除或未生效的历史事实冲突",
                    http_status=409,
                )
            return await repository.latest_event_id(candidate.uid, user_id=user_id)

    async def delete(
        self,
        uid: str,
        *,
        user_id: str | None,
        expected_version: int,
    ) -> None:
        """锁定并复核权威版本后执行可审计软删除。"""
        async with MySQLDatabase.session() as session:
            repository = MemoryRepository(session)
            current = await repository.get_by_uid(
                uid, user_id=user_id, for_update=True
            )
            if current is None:
                raise DataAgentError(
                    "memory_not_found",
                    "memory_delete",
                    "记忆不存在",
                    http_status=404,
                )
            if current.detail.record_version != expected_version:
                raise DataAgentError(
                    "stale_memory",
                    "memory_delete",
                    "目标记忆版本已发生变化",
                    http_status=409,
                )
            await repository.soft_delete(current)


class MySQLMemorySearchStore:
    """以独立短事务实现检索权威回查和访问统计。"""

    async def find_exact(
        self,
        source: str,
        query: str,
        categories: set[str] | None,
        *,
        user_id: str | None,
        limit: int,
    ) -> list[str]:
        """返回 MySQL 精确基线候选。"""
        async with MySQLDatabase.session() as session:
            return await MemoryRepository(session).find_exact_query(
                source, query, categories, user_id=user_id, limit=limit
            )

    async def load_authority(
        self, uids: set[str], *, user_id: str | None
    ) -> list[MemoryDetail]:
        """批量读取活动权威详情。"""
        async with MySQLDatabase.session() as session:
            rows = await MemoryRepository(session).get_many_active(
                sorted(uids), user_id=user_id
            )
        return [row.detail for row in rows]

    async def pending_targets(
        self, uids: set[str]
    ) -> dict[str, set[MemoryIndexTarget]]:
        """读取候选尚未收敛的投影目标。"""
        async with MySQLDatabase.session() as session:
            return await MemoryIndexOutboxRepository(session).pending_targets(uids)

    async def record_access(
        self, uids: set[str], *, source: str, user_id: str | None
    ) -> None:
        """在独立事务中记录访问热度。"""
        async with MySQLDatabase.session() as session:
            await MemoryRepository(session).record_access(
                uids, source=source, user_id=user_id
            )


class MySQLMemoryProjectionWorkStore:
    """以短事务实现投影领取、authority 复核和目标独立结算。"""

    async def claim(self, limit: int) -> list[MemoryOutboxItem]:
        """领取并提交一个有界期望状态批次。"""
        async with MySQLDatabase.session() as session:
            return await MemoryIndexOutboxRepository(session).claim_outbox(limit)

    async def prepare(self, item: MemoryOutboxItem) -> PreparedMemoryProjection:
        """在同一短事务内续租并读取当前权威投影。"""
        async with MySQLDatabase.session() as session:
            repository = MemoryIndexOutboxRepository(session)
            authority_held = await repository.renew_claim(item)
            projection = (
                await repository.projection(item.memory_uid)
                if authority_held
                else None
            )
        return PreparedMemoryProjection(authority_held, projection)

    async def settle_success(
        self,
        item: MemoryOutboxItem,
        *,
        content_hash: str | None,
    ) -> bool:
        """确认实际写入；authority 变化时原子登记持久收敛请求。"""
        async with MySQLDatabase.session() as session:
            repository = MemoryIndexOutboxRepository(session)
            acknowledged = await repository.acknowledge_outbox(
                item,
                content_hash=content_hash,
            )
            if not acknowledged:
                await repository.enqueue_convergence(item.memory_uid, item.target)
        return acknowledged

    async def settle_failure(
        self,
        item: MemoryOutboxItem,
        *,
        error_type: str,
        max_backoff_seconds: int,
    ) -> None:
        """在独立短事务内记录当前目标的安全错误类型与退避。"""
        async with MySQLDatabase.session() as session:
            await MemoryIndexOutboxRepository(session).retry_outbox(
                item,
                error_type,
                max_backoff_seconds,
            )

    async def dead_letter_count(self) -> int:
        """读取停止领取的死信积压数量。"""
        async with MySQLDatabase.session() as session:
            return await MemoryIndexOutboxRepository(session).dead_letter_count()


class MySQLMemoryMaintenanceStore:
    """以独立短事务实现到期和 tombstone 后安全物理清理。"""

    async def expire_due(self) -> int:
        """失效到期权威记忆并原子登记双目标删除。"""
        async with MySQLDatabase.session() as session:
            return await MemoryRepository(session).expire_due()

    async def purge_ready_user_memories(self) -> int:
        """仅物理清理双目标删除均已确认的用户记忆。"""
        async with MySQLDatabase.session() as session:
            return await MemoryRepository(session).purge_ready_user_memories()
