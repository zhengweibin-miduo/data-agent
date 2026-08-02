"""DDL 记忆内容引用的 Meta 对象校验 adapter。"""

from sqlalchemy.ext.asyncio import AsyncSession

from ddl_metadata.persistence.metadata_repository import MetadataRepository
from errors import DataAgentError
from models.memory import (
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
        # 步骤一：按记忆内容类型提取表、列和指标引用，形成期望对象集合。
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
        # 步骤二：在调用方事务内批量查询 Meta 权威表，避免逐引用往返。
        found = await MetadataRepository(session).existing_object_ids(
            table_ids,
            column_ids,
            metric_ids,
        )
        # 步骤三：对比期望与实际集合，统一报告所有不存在的结构化引用。
        if found != expected:
            raise DataAgentError(
                "unknown_meta_reference",
                "memory_update",
                "修正内容引用了不存在的 Meta 对象",
                details={"ids": ",".join(sorted(expected - found))},
            )
