"""四张 Meta 业务表的原子快照同步。"""

from collections.abc import Iterable

from sqlalchemy import Table, delete, exists, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.ddl_metadata.persistence.tables import (
    column_info,
    column_metric,
    metric_info,
    table_info,
)
from data_agent.models.physical import PhysicalSchema
from data_agent.models.semantic import (
    MetricMetadata,
    SemanticMetadata,
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
        """同步本次提交表范围内的快照，并清理该范围产生的陈旧关系。

        已存在列与当前列的并集定义唯一清理边界，确保删除的列及其旧指标
        关联可被发现，同时不会触碰未包含在本次 DDL 中的其他表。
        """
        # 步骤一：建立语义映射和提交表范围，并合并数据库旧列与当前列作为
        # 唯一清理边界，同时收集旧关联涉及的指标。
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

        # 步骤二：依次 upsert 当前表、列和指标内容，保留静态表定义与绑定参数。
        await self._upsert(
            table_info,
            [
                {
                    "id": table.id,
                    "name": table.qualified_name,
                    "role": semantic_tables[table.id].role.value,
                    "description": semantic_tables[table.id].description,
                    "alias": semantic_tables[table.id].aliases,
                }
                for table in schema.tables
            ],
            ("name", "role", "description", "alias"),
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
                    "index_profile": semantic_columns[column.id].value_index.model_dump(
                        mode="json"
                    ),
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
                "index_profile",
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

        # 步骤三：先移除范围内旧关联再按当前指标重建，避免指标列集合收缩后残留边；
        # 范围外列的关联不参与本次快照同步。
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

        # 步骤四：删除本次提交表中已经消失的列，范围外表始终不参与清理。
        stale_column_ids = existing_column_ids - current_column_ids
        if stale_column_ids:
            await self._session.execute(
                delete(column_info).where(column_info.c.id.in_(stale_column_ids))
            )

        impacted_metric_ids.update(metric.id for metric in metrics)
        if impacted_metric_ids:
            # 步骤五：只检查受本次关联重建影响的指标，且仅删除已经没有任何列引用的
            # 孤儿；仍被范围外列使用的共享指标必须保留。
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

    async def semantic_scope_before_sync(
        self,
        schema: PhysicalSchema,
    ) -> tuple[set[str], set[str]]:
        """返回本次表范围内同步前的列与关联指标标识。"""
        table_ids = {table.id for table in schema.tables}
        column_ids = set(
            (
                await self._session.scalars(
                    select(column_info.c.id).where(
                        column_info.c.table_id.in_(table_ids)
                    )
                )
            ).all()
        )
        if not column_ids:
            return column_ids, set()
        metric_ids = set(
            (
                await self._session.scalars(
                    select(column_metric.c.metric_id).where(
                        column_metric.c.column_id.in_(column_ids)
                    )
                )
            ).all()
        )
        return column_ids, metric_ids

    async def fingerprint_expiration_memory_keys(
        self,
        schema: PhysicalSchema,
        metrics: list[MetricMetadata],
    ) -> set[str]:
        """查询本次提交范围内所有可能需要指纹失效的记忆键。"""
        # 步骤一：收集本次提交表和当前列，建立最新快照的对象范围。
        submitted_table_ids = {table.id for table in schema.tables}
        current_column_ids = {
            column.id for table in schema.tables for column in table.columns
        }
        # 步骤二：读取提交表原有列，并与当前列合并以覆盖新增和删除两类变化。
        existing_column_ids = set(
            (
                await self._session.scalars(
                    select(column_info.c.id).where(
                        column_info.c.table_id.in_(submitted_table_ids)
                    )
                )
            ).all()
        )
        scoped_column_ids = existing_column_ids | current_column_ids
        # 步骤三：以当前指标为基线，并补入旧列关联的历史指标。
        impacted_metric_ids = {metric.id for metric in metrics}
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
        # 步骤四：返回表、列和指标记忆键的并集，限制后续指纹过期扫描范围。
        return submitted_table_ids | scoped_column_ids | impacted_metric_ids

    async def existing_object_ids(
        self,
        table_ids: set[str],
        column_ids: set[str],
        metric_ids: set[str],
    ) -> set[str]:
        """查询修正内容引用的现有 Meta 对象。"""
        # 步骤一：初始化统一结果集，后续按对象类型分别查询静态权威表。
        found: set[str] = set()
        # 步骤二：批量读取请求引用的表标识。
        if table_ids:
            found.update(
                (
                    await self._session.scalars(
                        select(table_info.c.id).where(table_info.c.id.in_(table_ids))
                    )
                ).all()
            )
        # 步骤三：批量读取请求引用的列标识。
        if column_ids:
            found.update(
                (
                    await self._session.scalars(
                        select(column_info.c.id).where(column_info.c.id.in_(column_ids))
                    )
                ).all()
            )
        # 步骤四：批量读取请求引用的指标标识。
        if metric_ids:
            found.update(
                (
                    await self._session.scalars(
                        select(metric_info.c.id).where(metric_info.c.id.in_(metric_ids))
                    )
                ).all()
            )
        # 步骤五：返回实际存在标识，由调用方统一比较缺失引用。
        return found
