"""领域安全的 Mem0 风格记忆 API 服务。"""

from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.identifiers import metric_id
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.ddl_metadata.memory.application.search import MemorySearchService
from data_agent.ddl_metadata.memory.domain.payloads import (
    content_object_ids,
    memory_content_hash,
)
from data_agent.ddl_metadata.memory.mysql.repository import (
    MemoryRepository,
    StoredMemory,
)
from data_agent.ddl_metadata.models.memory import (
    MemoryContent,
    MemoryDeleteResponse,
    MemoryDetail,
    MemoryHistoryPage,
    MemoryKind,
    MemorySearchResponse,
    MemoryStatus,
    MemoryUpdateResponse,
    MetricDefinitionContent,
    SemanticDecisionContent,
)
from data_agent.ddl_metadata.persistence.metadata_repository import MetadataRepository
from data_agent.infrastructure.mysql import MySQLDatabase


class MemoryService:
    """提供 search/get/history/update/delete 且不暴露任意 add。"""

    def __init__(self, jobs: DDLJobStore) -> None:
        """绑定与 DDL 工作流共享来源租约的任务存储。"""
        self._jobs = jobs
        self._search = MemorySearchService()

    async def search(
        self,
        query: str,
        source: str,
        *,
        kinds: set[MemoryKind] | None,
        limit: int,
    ) -> MemorySearchResponse:
        """执行有界混合检索并回查 MySQL 权威内容。"""
        return await self._search.search(
            query,
            source,
            kinds=kinds,
            limit=limit,
        )

    async def get(self, uid: str) -> MemoryDetail:
        """按稳定 UID 从 MySQL 读取权威详情。"""
        async with MySQLDatabase.session() as session:
            memory = await MemoryRepository(session).get_by_uid(uid)
        if memory is None:
            raise DDLMetadataError(
                "memory_not_found",
                "memory_get",
                "记忆不存在",
                http_status=404,
            )
        return memory.detail

    async def history(
        self,
        uid: str,
        *,
        offset: int,
        limit: int,
    ) -> MemoryHistoryPage:
        """读取有界只追加历史。"""
        async with MySQLDatabase.session() as session:
            page = await MemoryRepository(session).history(
                uid,
                offset=offset,
                limit=limit,
            )
        if page is None:
            raise DDLMetadataError(
                "memory_not_found",
                "memory_history",
                "记忆不存在",
                http_status=404,
            )
        return page

    async def update(
        self,
        uid: str,
        content: MemoryContent,
    ) -> MemoryUpdateResponse:
        """记录用户确认修正，要求完整 DDL 重处理后再成为活动事实。"""
        target = await self._get_stored(uid)
        if target.detail.status != MemoryStatus.ACTIVE:
            raise DDLMetadataError(
                "deleted_memory",
                "memory_update",
                "只能修正活动记忆",
                http_status=409,
            )
        normalized = self._normalize_content(target.detail, content)
        if memory_content_hash(normalized) == target.detail.content_hash:
            raise DDLMetadataError(
                "unchanged_update",
                "memory_update",
                "修正内容与当前事实相同",
                http_status=409,
            )
        async with self._jobs.mutation_lease(target.detail.source):
            async with MySQLDatabase.session() as session:
                repository = MemoryRepository(session)
                current = await repository.get_by_uid(uid)
                if current is None or current.detail.status != MemoryStatus.ACTIVE:
                    raise DDLMetadataError(
                        "stale_memory",
                        "memory_update",
                        "目标记忆已发生变化",
                        http_status=409,
                    )
                pending = await repository.latest_user_update(current.id)
                if (
                    pending is not None
                    and memory_content_hash(pending) == memory_content_hash(normalized)
                ):
                    raise DDLMetadataError(
                        "unchanged_update",
                        "memory_update",
                        "修正内容与待重处理内容相同",
                        http_status=409,
                    )
                await self._validate_meta_references(session, normalized)
                event_id = await repository.append_user_update(
                    current,
                    normalized,
                )
        return MemoryUpdateResponse(memory_uid=uid, event_id=event_id)

    async def delete(self, uid: str) -> MemoryDeleteResponse:
        """在来源租约内执行可审计软删除。"""
        target = await self._get_stored(uid)
        async with self._jobs.mutation_lease(target.detail.source):
            async with MySQLDatabase.session() as session:
                repository = MemoryRepository(session)
                current = await repository.get_by_uid(uid)
                if current is None:
                    raise DDLMetadataError(
                        "memory_not_found",
                        "memory_delete",
                        "记忆不存在",
                        http_status=404,
                    )
                await repository.soft_delete(current)
        return MemoryDeleteResponse(memory_uid=uid)

    async def _get_stored(self, uid: str) -> StoredMemory:
        """读取内部主键与权威详情。"""
        async with MySQLDatabase.session() as session:
            memory = await MemoryRepository(session).get_by_uid(uid)
        if memory is None:
            raise DDLMetadataError(
                "memory_not_found",
                "memory_get",
                "记忆不存在",
                http_status=404,
            )
        return memory

    def _normalize_content(
        self,
        target: MemoryDetail,
        content: MemoryContent,
    ) -> MemoryContent:
        """固定 kind 和对象身份，阻止修正越过原作用域。"""
        if MemoryKind(content.kind) != target.kind:
            raise DDLMetadataError(
                "memory_kind_conflict",
                "memory_update",
                "修正内容类型必须与目标一致",
                http_status=409,
            )
        if isinstance(content, SemanticDecisionContent):
            if content_object_ids(content) != [target.scope_key]:
                raise DDLMetadataError(
                    "memory_scope_conflict",
                    "memory_update",
                    "语义修正必须保留目标对象 ID",
                    http_status=409,
                )
            return content.model_copy(update={"trust": "user_confirmed"})
        if not isinstance(content, MetricDefinitionContent):
            raise DDLMetadataError(
                "immutable_memory_kind",
                "memory_update",
                "问题和回答事实不可直接修正",
                http_status=409,
            )
        metric = content.metric
        if (metric.id and metric.id != target.scope_key) or metric_id(
            target.source,
            metric.fact_table_id,
            metric.name,
        ) != target.scope_key:
            raise DDLMetadataError(
                "memory_scope_conflict",
                "memory_update",
                "指标修正必须保留事实表、名称和指标 ID",
                http_status=409,
            )
        return content.model_copy(
            update={
                "trust": "user_confirmed",
                "metric": metric.model_copy(update={"id": target.scope_key}),
            }
        )

    async def _validate_meta_references(
        self,
        session: AsyncSession,
        content: MemoryContent,
    ) -> None:
        """确认结构化修正引用的 Meta 对象当前存在。"""
        table_ids: set[str] = set()
        column_ids: set[str] = set()
        metric_ids: set[str] = set()
        if isinstance(content, SemanticDecisionContent):
            if content.table is not None:
                table_ids.add(content.table.table_id)
            elif content.column is not None:
                column_ids.add(content.column.column_id)
        elif isinstance(content, MetricDefinitionContent):
            table_ids.add(content.metric.fact_table_id)
            column_ids.update(content.metric.relevant_column_ids)
            metric_ids.add(content.metric.id)
        expected = table_ids | column_ids | metric_ids
        found = await MetadataRepository(session).existing_object_ids(
            table_ids,
            column_ids,
            metric_ids,
        )
        if found != expected:
            raise DDLMetadataError(
                "unknown_meta_reference",
                "memory_update",
                "修正内容引用了不存在的 Meta 对象",
                details={"ids": ",".join(sorted(expected - found))},
            )
