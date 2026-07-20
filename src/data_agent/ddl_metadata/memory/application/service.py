"""领域安全的 Mem0 风格记忆 API 服务。"""

from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.identifiers import memory_uid as build_memory_uid
from data_agent.ddl_metadata.identifiers import metric_id
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.ddl_metadata.memory.application.search import MemorySearchService
from data_agent.ddl_metadata.memory.domain.payloads import (
    build_memory_text,
    canonical_content_json,
    content_object_ids,
    memory_content_hash,
)
from data_agent.ddl_metadata.memory.mysql.repository import (
    MemoryRepository,
    StoredMemory,
)
from data_agent.ddl_metadata.models.memory import (
    MemoryCandidate,
    MemoryContent,
    MemoryDeleteResponse,
    MemoryDetail,
    MemoryHistoryPage,
    MemoryKind,
    MemorySearchResponse,
    MemoryStatus,
    MemoryTrust,
    MemoryUpdateResponse,
    MetricDefinitionContent,
    SemanticDecisionContent,
    UserMemoryContent,
)
from data_agent.ddl_metadata.persistence.metadata_repository import MetadataRepository
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.settings import app_config

_CONVERSATION_SOURCE = "data_agent_conversation"


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

    async def search_user(
        self,
        user_id: str,
        query: str,
        *,
        limit: int,
    ) -> MemorySearchResponse:
        """只搜索指定用户的跨会话长期记忆。"""
        return await self._search.search(
            query,
            _CONVERSATION_SOURCE,
            user_id=user_id,
            kinds={MemoryKind.USER_MEMORY},
            limit=limit,
        )

    async def get(
        self,
        uid: str,
        *,
        user_id: str | None = None,
    ) -> MemoryDetail:
        """按稳定 UID 从 MySQL 读取权威详情。"""
        async with MySQLDatabase.session() as session:
            memory = await MemoryRepository(session).get_by_uid(
                uid,
                user_id=user_id,
            )
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
        user_id: str | None = None,
        offset: int,
        limit: int,
    ) -> MemoryHistoryPage:
        """读取有界只追加历史。"""
        async with MySQLDatabase.session() as session:
            page = await MemoryRepository(session).history(
                uid,
                user_id=user_id,
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
        *,
        user_id: str | None = None,
    ) -> MemoryUpdateResponse:
        """记录用户确认修正，要求完整 DDL 重处理后再成为活动事实。"""
        target = await self._get_stored(uid, user_id=user_id)
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
        if user_id is not None:
            return await self._update_user_memory(target, normalized, user_id)
        async with self._jobs.mutation_lease(target.detail.source):
            async with MySQLDatabase.session() as session:
                repository = MemoryRepository(session)
                current = await repository.get_by_uid(uid, user_id=user_id)
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

    async def delete(
        self,
        uid: str,
        *,
        user_id: str | None = None,
    ) -> MemoryDeleteResponse:
        """在来源租约内执行可审计软删除。"""
        target = await self._get_stored(uid, user_id=user_id)
        if user_id is not None:
            async with MySQLDatabase.session() as session:
                current = await MemoryRepository(session).get_by_uid(
                    uid,
                    user_id=user_id,
                    for_update=True,
                )
                if current is None:
                    raise DDLMetadataError(
                        "memory_not_found",
                        "memory_delete",
                        "记忆不存在",
                        http_status=404,
                    )
                await MemoryRepository(session).soft_delete(current)
            return MemoryDeleteResponse(memory_uid=uid)
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

    async def _get_stored(
        self,
        uid: str,
        *,
        user_id: str | None = None,
    ) -> StoredMemory:
        """读取内部主键与权威详情。"""
        async with MySQLDatabase.session() as session:
            memory = await MemoryRepository(session).get_by_uid(
                uid,
                user_id=user_id,
            )
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
        if isinstance(content, UserMemoryContent):
            if not isinstance(target.content, UserMemoryContent):
                raise DDLMetadataError(
                    "memory_kind_conflict",
                    "memory_update",
                    "目标记忆内容类型不一致",
                    http_status=409,
                )
            if (
                content.category != target.content.category
                or content.key.casefold() != target.content.key.casefold()
            ):
                raise DDLMetadataError(
                    "memory_scope_conflict",
                    "memory_update",
                    "用户记忆修正必须保留类别和键",
                    http_status=409,
                )
            return content.model_copy(
                update={
                    "supporting_user_quote": (
                        target.content.supporting_user_quote
                    ),
                    "evidence_message_uids": (
                        target.content.evidence_message_uids
                    ),
                    "confirmed_assistant_message_uid": (
                        target.content.confirmed_assistant_message_uid
                    ),
                }
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

    async def _update_user_memory(
        self,
        target: StoredMemory,
        content: MemoryContent,
        user_id: str,
    ) -> MemoryUpdateResponse:
        """用新的用户确认事实替代旧活动记忆。"""
        if not isinstance(content, UserMemoryContent):
            raise DDLMetadataError(
                "immutable_memory_kind",
                "memory_update",
                "用户路由只允许修正跨会话用户记忆",
                http_status=409,
            )
        content_json = canonical_content_json(content)
        uid = build_memory_uid(
            f"{_CONVERSATION_SOURCE}:{user_id}",
            MemoryKind.USER_MEMORY.value,
            target.detail.scope_key,
            target.detail.schema_fingerprint,
            content_json,
        )
        candidate = MemoryCandidate(
            uid=uid,
            source=_CONVERSATION_SOURCE,
            user_id=user_id,
            created_conversation_uid=target.detail.created_conversation_uid,
            created_message_uid=target.detail.created_message_uid,
            kind=MemoryKind.USER_MEMORY,
            scope_key=target.detail.scope_key,
            schema_fingerprint=target.detail.schema_fingerprint,
            memory_text=build_memory_text(content),
            content=content,
            content_hash=memory_content_hash(content),
            trust=MemoryTrust.USER_CONFIRMED,
            content_version=app_config.memory.content_version,
            projection_version=app_config.memory.projection_version,
            supersedes_uids=[target.detail.uid],
        )
        async with MySQLDatabase.session() as session:
            repository = MemoryRepository(session)
            current = await repository.get_by_uid(
                target.detail.uid,
                user_id=user_id,
                for_update=True,
            )
            if current is None or current.detail.status != MemoryStatus.ACTIVE:
                raise DDLMetadataError(
                    "stale_memory",
                    "memory_update",
                    "目标记忆已发生变化",
                    http_status=409,
                )
            await repository.upsert_candidates([candidate])
            history = await repository.history(
                target.detail.uid,
                user_id=user_id,
                offset=0,
                limit=100,
            )
        event_id = history.items[-1].id if history and history.items else 0
        return MemoryUpdateResponse(memory_uid=uid, event_id=event_id)

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
