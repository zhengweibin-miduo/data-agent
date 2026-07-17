"""浏览器长期记忆管理服务。"""

from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.identifiers import metric_id
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.ddl_metadata.memory.payloads import content_object_ids
from data_agent.ddl_metadata.memory.snapshots import upsert_correction
from data_agent.ddl_metadata.models import (
    MemoryContent,
    MemoryCorrectionResponse,
    MemoryDetail,
    MemoryKind,
    MemoryPage,
    MemoryPatchRequest,
    MemoryRowStatus,
    MetricDefinitionContent,
    SemanticDecisionContent,
)
from data_agent.ddl_metadata.persistence.memory_repository import MemoryRepository
from data_agent.ddl_metadata.persistence.metadata_repository import MetadataRepository
from data_agent.infrastructure.mysql import MySQLDatabase


class MemoryService:
    """提供有界查询、幂等管理与追加式修正。"""

    def __init__(self, jobs: DDLJobStore) -> None:
        """绑定与记忆变更共享来源租约的任务存储。"""
        self._jobs = jobs

    async def list_page(
        self,
        source: str,
        *,
        kind: MemoryKind | None,
        row_status: MemoryRowStatus,
        pinned: bool | None,
        limit: int,
        cursor: str | None,
    ) -> MemoryPage:
        """查询一个稳定游标页。"""
        async with MySQLDatabase.session() as session:
            return await MemoryRepository(session).list_page(
                source,
                kind=kind,
                row_status=row_status,
                pinned=pinned,
                limit=limit,
                cursor=cursor,
            )

    async def get_detail(self, uid: str) -> MemoryDetail:
        """按稳定 UID 查询详情。"""
        async with MySQLDatabase.session() as session:
            detail = await MemoryRepository(session).get_detail(uid)
        if detail is None:
            raise DDLMetadataError(
                "memory_not_found",
                "memory_detail",
                "记忆不存在",
                http_status=404,
            )
        return detail

    async def patch(
        self,
        uid: str,
        request: MemoryPatchRequest,
    ) -> MemoryDetail:
        """在来源租约保护下幂等 pin/unpin 或归档。"""
        target = await self.get_detail(uid)
        async with self._jobs.mutation_lease(target.source):
            async with MySQLDatabase.session() as session:
                return await MemoryRepository(session).patch(
                    uid,
                    pinned=request.pinned,
                    archive=request.row_status == MemoryRowStatus.ARCHIVED,
                )

    async def correct(
        self,
        uid: str,
        content: MemoryContent,
    ) -> MemoryCorrectionResponse:
        """追加用户确认替代项并原子归档旧记忆。"""
        target = await self.get_detail(uid)
        if target.row_status != MemoryRowStatus.NORMAL:
            raise DDLMetadataError(
                "archived_memory",
                "memory_correction",
                "只能修正活动记忆",
                http_status=409,
            )
        if target.kind not in {
            MemoryKind.SEMANTIC_DECISION,
            MemoryKind.METRIC_DEFINITION,
        }:
            raise DDLMetadataError(
                "immutable_memory_kind",
                "memory_correction",
                "问题和回答记忆不可修改",
                http_status=409,
            )
        if MemoryKind(content.kind) != target.kind:
            raise DDLMetadataError(
                "memory_kind_conflict",
                "memory_correction",
                "修正内容类型必须与目标一致",
                http_status=409,
            )
        normalized = self._normalize_content(target, content)
        async with self._jobs.mutation_lease(target.source):
            async with MySQLDatabase.session() as session:
                repository = MemoryRepository(session)
                current = await repository.get_by_uid(uid)
                if current is None or current.item.row_status != MemoryRowStatus.NORMAL:
                    raise DDLMetadataError(
                        "stale_memory",
                        "memory_correction",
                        "目标记忆已发生变化",
                        http_status=409,
                    )
                await self._validate_meta_references(session, normalized)
                candidate = await upsert_correction(
                    session,
                    current,
                    normalized,
                )
        return MemoryCorrectionResponse(
            memory_uid=candidate.uid,
            supersedes_uid=uid,
        )

    def _normalize_content(
        self,
        target: MemoryDetail,
        content: MemoryContent,
    ) -> MemoryContent:
        """固定目标对象身份，阻止修正越过原作用域。"""
        if isinstance(content, SemanticDecisionContent):
            object_ids = content_object_ids(content)
            if object_ids != [target.scope_key]:
                raise DDLMetadataError(
                    "memory_scope_conflict",
                    "memory_correction",
                    "语义修正必须保留目标对象 ID",
                    http_status=409,
                )
            return content
        if not isinstance(content, MetricDefinitionContent):
            raise DDLMetadataError(
                "immutable_memory_kind",
                "memory_correction",
                "该记忆类型不可修正",
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
                "memory_correction",
                "指标修正必须保留目标事实表、名称和指标 ID",
                http_status=409,
            )
        return content.model_copy(
            update={"metric": metric.model_copy(update={"id": target.scope_key})}
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
                "memory_correction",
                "修正内容引用了不存在的 Meta 对象",
                details={"ids": ",".join(sorted(expected - found))},
            )
