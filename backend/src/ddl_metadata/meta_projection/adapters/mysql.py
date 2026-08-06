"""Meta Projection application ports 的 MySQL 适配器。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

from data_sync.locks import generation_lock_name
from ddl_metadata.meta_projection.models import (
    ClaimedMetadataIndexWork,
    MetadataCandidate,
    MetadataIndexDesired,
    MetadataIndexTarget,
    MetadataObjectKind,
    MetadataSemanticHit,
    MetadataSemanticProjection,
    MetadataValueCandidate,
    MetadataValueProjection,
)
from ddl_metadata.meta_projection.projections import (
    MetadataProjectionRepository,
)
from ddl_metadata.meta_projection.repository import (
    MetadataIndexOutboxRepository,
)
from infrastructure.mysql import MySQLDatabase


class MySQLProjectionWorkStore:
    """以短事务和 generation lock 实现投影工作持久化端口。"""

    def __init__(self, *, generation_lock_timeout_seconds: int) -> None:
        """绑定投影锁超时配置。"""
        if generation_lock_timeout_seconds <= 0:
            raise ValueError("Meta Projection generation lock 超时必须为正整数")
        self._generation_lock_timeout_seconds = generation_lock_timeout_seconds

    async def claim(self, limit: int) -> list[ClaimedMetadataIndexWork]:
        """在短事务中领取有界工作并提交租约。"""
        async with MySQLDatabase.session() as session:
            return await MetadataIndexOutboxRepository(session).claim(limit)

    @asynccontextmanager
    async def authority(
        self,
        item: ClaimedMetadataIndexWork,
    ) -> AsyncIterator[bool]:
        """持有投影锁，短事务续租后在无事务状态执行远程工作。"""
        lock_scope = (
            "metadata-values"
            if item.target == MetadataIndexTarget.VALUES
            else f"metadata-semantic-{item.object_kind.value}"
        )
        locks = {
            generation_lock_name("metadata-index-rebuild", "all"),
            generation_lock_name(lock_scope, item.object_id),
        }
        # 步骤一：generation lock 串行化同一对象与全局重建。
        async with MySQLDatabase.advisory_locks(
            locks,
            timeout_seconds=self._generation_lock_timeout_seconds,
        ):
            # 步骤二：续租事务先提交，再把权威判断交给无事务的远程阶段。
            async with MySQLDatabase.session() as session:
                authoritative = await MetadataIndexOutboxRepository(
                    session
                ).renew_lease(item)
            yield authoritative

    async def enqueue(self, desired: Sequence[MetadataIndexDesired]) -> None:
        """在一个短事务中合并一批 desired state。"""
        async with MySQLDatabase.session() as session:
            await MetadataIndexOutboxRepository(session).enqueue(list(desired))

    async def acknowledge(self, item: ClaimedMetadataIndexWork) -> bool:
        """按完整 desired identity 确认成功。"""
        async with MySQLDatabase.session() as session:
            return await MetadataIndexOutboxRepository(session).acknowledge(item)

    async def restore_reconciliation(self, item: ClaimedMetadataIndexWork) -> bool:
        """恢复迟到写入对应 desired state 的可领取性。"""
        async with MySQLDatabase.session() as session:
            return await MetadataIndexOutboxRepository(session).restore_reconciliation(
                item
            )

    async def defer(self, item: ClaimedMetadataIndexWork) -> bool:
        """无损延后本地尚未就绪的工作。"""
        async with MySQLDatabase.session() as session:
            return await MetadataIndexOutboxRepository(session).defer(item)

    async def backoff(
        self,
        item: ClaimedMetadataIndexWork,
        error_type: str,
    ) -> bool:
        """为远程失败写入数据库时钟退避。"""
        async with MySQLDatabase.session() as session:
            return await MetadataIndexOutboxRepository(session).backoff(
                item,
                error_type,
            )

    async def dead_letter_count(self) -> int:
        """读取达到最大失败次数的工作数量。"""
        async with MySQLDatabase.session() as session:
            return await MetadataIndexOutboxRepository(session).dead_letter_count()


class MySQLProjectionReader:
    """以独立短事务回读 Meta 权威投影。"""

    async def semantic_projection(
        self,
        kind: MetadataObjectKind,
        object_id: str,
    ) -> MetadataSemanticProjection | None:
        """读取一个当前语义投影。"""
        async with MySQLDatabase.session() as session:
            return await MetadataProjectionRepository(session).semantic_projection(
                kind,
                object_id,
            )

    async def semantic_identities(self) -> list[tuple[MetadataObjectKind, str]]:
        """读取当前全部语义对象身份。"""
        async with MySQLDatabase.session() as session:
            return await MetadataProjectionRepository(session).semantic_identities()

    async def eligible_table_ids(self) -> set[str]:
        """读取当前需要字段值投影的表身份。"""
        async with MySQLDatabase.session() as session:
            return await MetadataProjectionRepository(session).eligible_table_ids()

    async def schema_is_authoritative(
        self, source: str, schema_fingerprint: str
    ) -> bool:
        """短事务核验完整 accepted schema 指纹。"""
        async with MySQLDatabase.session() as session:
            return await MetadataProjectionRepository(session).schema_is_authoritative(
                source, schema_fingerprint
            )

    async def authoritative_candidates(
        self,
        identities: list[MetadataSemanticHit],
        *,
        table_ids: set[str] | None = None,
        column_ids: set[str] | None = None,
    ) -> list[MetadataCandidate]:
        """按派生索引顺序回读当前 Meta 候选。"""
        async with MySQLDatabase.session() as session:
            return await MetadataProjectionRepository(session).authoritative_candidates(
                identities, table_ids=table_ids, column_ids=column_ids
            )

    async def resolve_value_scope(
        self,
        column_ids: set[str],
    ) -> tuple[dict[str, tuple[str, str]], bool]:
        """解析字段值查询的当前权威范围与完整性。"""
        async with MySQLDatabase.session() as session:
            return await MetadataProjectionRepository(session).resolve_value_scope(
                column_ids
            )

    async def authoritative_value_candidates(
        self,
        projections: list[MetadataValueProjection],
        scope: dict[str, tuple[str, str]],
    ) -> list[MetadataValueCandidate]:
        """拒绝越界或过期的字段值命中。"""
        async with MySQLDatabase.session() as session:
            return MetadataProjectionRepository(session).authoritative_value_candidates(
                projections, scope
            )
