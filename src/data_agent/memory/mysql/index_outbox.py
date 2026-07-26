"""记忆派生索引期望状态仓储。"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import RowMapping, delete, func, select, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.memory.domain.payloads import content_object_ids
from data_agent.memory.mysql.tables import (
    agent_memory,
    memory_index_outbox,
)
from data_agent.models.memory import (
    MEMORY_CONTENT_ADAPTER,
    MemoryIndexOperation,
    MemoryIndexTarget,
    MemoryLifecyclePolicy,
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
        # 步骤一：为每个权威 UID 展开两个派生目标的最新期望状态。
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
        # 步骤二：按 UID 与目标幂等覆盖状态，并重置重试进度。
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
        # 步骤一：批量读取指定 UID 尚未完成的目标行。
        rows = (
            await self._session.execute(
                select(
                    memory_index_outbox.c.memory_uid,
                    memory_index_outbox.c.target,
                ).where(memory_index_outbox.c.memory_uid.in_(uids))
            )
        ).all()
        # 步骤二：按权威 UID 聚合待确认目标，缺少记录表示该 UID 已无待办。
        result: dict[str, set[MemoryIndexTarget]] = {}
        for uid, target in rows:
            result.setdefault(str(uid), set()).add(MemoryIndexTarget(str(target)))
        return result

    async def claim_outbox(self, limit: int) -> list[MemoryOutboxItem]:
        """通过行锁有界领取可执行索引期望状态。"""
        # 步骤一：按可用时间跳锁领取有界任务，使并发 dispatcher 不重复处理。
        rows = (
            await self._session.execute(
                select(memory_index_outbox)
                .where(memory_index_outbox.c.available_at <= func.now())
                .order_by(memory_index_outbox.c.updated_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        ).mappings()
        # 步骤二：将锁定行转换为调用方可执行的类型化 outbox 项。
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
        # 步骤一：按完整期望条件确认，避免迟到 worker 删除后来覆盖的新状态。
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
        # 步骤一：根据已尝试次数计算有上限的指数退避时间。
        attempts = item.attempts + 1
        delay = min(2 ** min(attempts, 20), max_backoff_seconds)
        available_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=delay)
        # 步骤二：仅更新同一 UID 与目标的重试元数据，并截断安全异常类型。
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
        # 步骤一：按 UID 读取 MySQL 权威行，禁止从派生索引反向构造事实。
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
        # 步骤二：严格解码类型化内容并构造两个索引共享的有界投影。
        content = MEMORY_CONTENT_ADAPTER.validate_python(row["content"])
        return MemoryProjection(
            memory_uid=str(row["uid"]),
            source=str(row["source"]),
            user_id=(str(row["user_id"]) if row["user_id"] is not None else None),
            created_conversation_uid=(
                str(row["created_conversation_uid"])
                if row["created_conversation_uid"] is not None
                else None
            ),
            created_message_uid=(
                str(row["created_message_uid"])
                if row["created_message_uid"] is not None
                else None
            ),
            category=str(row["category"]),
            memory_key=str(row["memory_key"]),
            content_schema=str(row["content_schema"]),
            schema_fingerprint=(
                str(row["schema_fingerprint"])
                if row["schema_fingerprint"] is not None
                else None
            ),
            memory_text=str(row["memory_text"]),
            content_hash=str(row["content_hash"]),
            object_ids=content_object_ids(content),
            trust=MemoryTrust(str(row["trust"])),
            status=MemoryStatus(str(row["status"])),
            importance_score=float(row["importance_score"]),
            lifecycle_policy=MemoryLifecyclePolicy(str(row["lifecycle_policy"])),
            expires_at=row["expires_at"],
            record_version=int(row["record_version"]),
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
        # 步骤一：按排他主键游标读取有界 ACTIVE 行，供重建任务稳定续扫。
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
        if not uids:
            return
        # 步骤一：仅推进当前 ACTIVE 权威行的投影版本。
        await self._session.execute(
            update(agent_memory)
            .where(
                agent_memory.c.uid.in_(uids),
                agent_memory.c.status == MemoryStatus.ACTIVE.value,
            )
            .values(projection_version=app_config.memory.projection_version)
        )
        # 步骤二：为同批 UID 重建两个目标的 UPSERT 期望。
        await self.set_desired_state(uids, MemoryIndexOperation.UPSERT)
