"""四张 Meta 业务表的原子快照同步。"""

from collections.abc import Iterable

from sqlalchemy import Table, delete, exists, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.ddl_metadata.models import (
    MetricMetadata,
    PhysicalSchema,
    SemanticMetadata,
)
from data_agent.ddl_metadata.persistence.tables import (
    column_info,
    column_metric,
    metric_info,
    table_info,
)


class MetadataRepository:
    """在调用方提供的事务内同步 Meta 快照。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定由调用方管理事务边界的 Session。"""
        self._session = session

    async def _upsert(
        self,
        table: Table,
        rows: list[dict[str, object]],
        update_columns: Iterable[str],
    ) -> None:
        """执行静态表上的 MySQL 批量 upsert。"""
        if not rows:
            return
        statement = insert(table).values(rows)
        await self._session.execute(
            statement.on_duplicate_key_update(
                **{column: statement.inserted[column] for column in update_columns}
            )
        )

    async def synchronize(
        self,
        schema: PhysicalSchema,
        metadata: SemanticMetadata,
        metrics: list[MetricMetadata],
    ) -> None:
        """同步提交表范围内的当前快照并清理范围内陈旧数据。"""
        semantic_tables = {table.table_id: table for table in metadata.tables}
        semantic_columns = {column.column_id: column for column in metadata.columns}
        submitted_table_ids = [table.id for table in schema.tables]
        existing_column_ids = set(
            (
                await self._session.scalars(
                    select(column_info.c.id).where(
                        column_info.c.table_id.in_(submitted_table_ids)
                    )
                )
            ).all()
        )
        current_column_ids = {
            column.id for table in schema.tables for column in table.columns
        }
        scoped_column_ids = existing_column_ids | current_column_ids
        impacted_metric_ids: set[str] = set()
        if scoped_column_ids:
            impacted_metric_ids.update(
                (
                    await self._session.scalars(
                        select(column_metric.c.metric_id).where(
                            column_metric.c.column_id.in_(scoped_column_ids)
                        )
                    )
                ).all()
            )

        await self._upsert(
            table_info,
            [
                {
                    "id": table.id,
                    "name": table.qualified_name,
                    "role": semantic_tables[table.id].role.value,
                    "description": semantic_tables[table.id].description,
                }
                for table in schema.tables
            ],
            ("name", "role", "description"),
        )
        await self._upsert(
            column_info,
            [
                {
                    "id": column.id,
                    "name": column.name,
                    "type": column.data_type,
                    "role": semantic_columns[column.id].role.value,
                    "examples": [],
                    "description": semantic_columns[column.id].description,
                    "alias": semantic_columns[column.id].aliases,
                    "table_id": table.id,
                }
                for table in schema.tables
                for column in table.columns
            ],
            (
                "name",
                "type",
                "role",
                "examples",
                "description",
                "alias",
                "table_id",
            ),
        )
        await self._upsert(
            metric_info,
            [
                {
                    "id": metric.id,
                    "name": metric.name,
                    "description": metric.definition,
                    "relevant_columns": metric.relevant_column_ids,
                    "alias": metric.aliases,
                }
                for metric in metrics
            ],
            ("name", "description", "relevant_columns", "alias"),
        )

        if scoped_column_ids:
            await self._session.execute(
                delete(column_metric).where(
                    column_metric.c.column_id.in_(scoped_column_ids)
                )
            )
        links = [
            {
                "column_id": column_identifier,
                "metric_id": metric.id,
            }
            for metric in metrics
            for column_identifier in metric.relevant_column_ids
        ]
        if links:
            statement = insert(column_metric).values(links)
            await self._session.execute(
                statement.on_duplicate_key_update(
                    metric_id=statement.inserted.metric_id
                )
            )

        stale_column_ids = existing_column_ids - current_column_ids
        if stale_column_ids:
            await self._session.execute(
                delete(column_info).where(column_info.c.id.in_(stale_column_ids))
            )

        impacted_metric_ids.update(metric.id for metric in metrics)
        if impacted_metric_ids:
            await self._session.execute(
                delete(metric_info).where(
                    metric_info.c.id.in_(impacted_metric_ids),
                    ~exists(
                        select(column_metric.c.metric_id).where(
                            column_metric.c.metric_id == metric_info.c.id
                        )
                    ),
                )
            )

    async def existing_object_ids(
        self,
        table_ids: set[str],
        column_ids: set[str],
        metric_ids: set[str],
    ) -> set[str]:
        """查询修正内容引用的现有 Meta 对象。"""
        found: set[str] = set()
        if table_ids:
            found.update(
                (
                    await self._session.scalars(
                        select(table_info.c.id).where(table_info.c.id.in_(table_ids))
                    )
                ).all()
            )
        if column_ids:
            found.update(
                (
                    await self._session.scalars(
                        select(column_info.c.id).where(column_info.c.id.in_(column_ids))
                    )
                ).all()
            )
        if metric_ids:
            found.update(
                (
                    await self._session.scalars(
                        select(metric_info.c.id).where(metric_info.c.id.in_(metric_ids))
                    )
                ).all()
            )
        return found
