"""DDL 记忆内容引用的 Meta 对象校验 adapter。"""

from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.ddl_metadata.persistence.metadata_repository import MetadataRepository
from data_agent.errors import DataAgentError
from data_agent.models.memory import (
    MemoryContent,
    MetricDefinitionContent,
    SemanticDecisionContent,
)


class MetadataMemoryReferenceValidator:
    """验证 DDL 记忆引用的表、列和指标仍存在。"""

    async def validate(
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
            raise DataAgentError(
                "unknown_meta_reference",
                "memory_update",
                "修正内容引用了不存在的 Meta 对象",
                details={"ids": ",".join(sorted(expected - found))},
            )
