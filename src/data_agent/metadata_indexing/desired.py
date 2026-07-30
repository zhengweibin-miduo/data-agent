"""Meta 与 DW 事务写入的索引期望状态。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.data_sync.models import DesiredSyncTable
from data_agent.ddl_metadata.persistence.tables import column_info
from data_agent.metadata_indexing.models import (
    MetadataIndexDesired,
    MetadataIndexOperation,
    MetadataIndexTarget,
    MetadataObjectKind,
)
from data_agent.metadata_indexing.repository import (
    MetadataIndexOutboxRepository,
    metadata_desired_version,
)
from data_agent.models.physical import PhysicalSchema
from data_agent.models.semantic import MetricMetadata, SemanticMetadata
from data_agent.settings import app_config


def semantic_desired_states(
    schema: PhysicalSchema,
    metadata: SemanticMetadata,
    metrics: list[MetricMetadata],
    *,
    removed_columns: set[str] | None = None,
    removed_metrics: set[str] | None = None,
) -> list[MetadataIndexDesired]:
    """为当前已接受快照生成幂等语义 upsert/delete 期望状态。"""
    semantic_tables = {item.table_id: item for item in metadata.tables}
    semantic_columns = {item.column_id: item for item in metadata.columns}
    objects: list[tuple[MetadataObjectKind, str, object]] = []
    for table in schema.tables:
        objects.append(
            (
                MetadataObjectKind.TABLE,
                table.id,
                {
                    "physical": table.model_dump(mode="json"),
                    "semantic": semantic_tables[table.id].model_dump(mode="json"),
                },
            )
        )
        objects.extend(
            (
                MetadataObjectKind.COLUMN,
                column.id,
                {
                    "table_id": table.id,
                    "physical": column.model_dump(mode="json"),
                    "semantic": semantic_columns[column.id].model_dump(mode="json"),
                },
            )
            for column in table.columns
        )
    objects.extend(
        (MetadataObjectKind.METRIC, metric.id, metric.model_dump(mode="json"))
        for metric in metrics
    )
    semantic = [
        MetadataIndexDesired(
            target=MetadataIndexTarget.SEMANTIC,
            object_kind=kind,
            object_id=object_id,
            operation=MetadataIndexOperation.UPSERT,
            desired_version=metadata_desired_version(
                {
                    "schema_fingerprint": schema.schema_fingerprint,
                    "projection_version": app_config.metadata_index.projection_version,
                    "object": payload,
                }
            ),
        )
        for kind, object_id, payload in objects
    ]
    semantic.extend(
        MetadataIndexDesired(
            target=MetadataIndexTarget.SEMANTIC,
            object_kind=kind,
            object_id=object_id,
            operation=MetadataIndexOperation.DELETE,
            desired_version=metadata_desired_version(
                {
                    "operation": MetadataIndexOperation.DELETE.value,
                    "projection_version": app_config.metadata_index.projection_version,
                    "kind": kind.value,
                    "object_id": object_id,
                }
            ),
        )
        for kind, object_ids in (
            (MetadataObjectKind.COLUMN, removed_columns or set()),
            (MetadataObjectKind.METRIC, removed_metrics or set()),
        )
        for object_id in sorted(object_ids)
    )
    semantic.extend(
        MetadataIndexDesired(
            target=MetadataIndexTarget.VALUES,
            object_kind=MetadataObjectKind.TABLE,
            object_id=table.id,
            operation=MetadataIndexOperation.REFRESH,
            desired_version=metadata_desired_version(
                {
                    "schema_fingerprint": schema.schema_fingerprint,
                    "table_id": table.id,
                    "projection_version": app_config.metadata_index.projection_version,
                }
            ),
        )
        for table in schema.tables
    )
    return semantic


async def enqueue_value_refresh(
    session: AsyncSession,
    desired: DesiredSyncTable,
    version_payload: object,
) -> None:
    """在当前 DW 事务中合并一条表级字段值刷新。"""
    table_identifier = await session.scalar(
        select(column_info.c.table_id).where(
            column_info.c.id == desired.columns[0].id
        )
    )
    if table_identifier is None:
        raise RuntimeError("DW 字段未对应当前 Meta 表")
    await MetadataIndexOutboxRepository(session).enqueue(
        [
            MetadataIndexDesired(
                target=MetadataIndexTarget.VALUES,
                object_kind=MetadataObjectKind.TABLE,
                object_id=str(table_identifier),
                operation=MetadataIndexOperation.REFRESH,
                desired_version=metadata_desired_version(
                    {
                        "desired_hash": desired.desired_hash(),
                        "position": version_payload,
                    }
                ),
            )
        ],
        debounce_seconds=app_config.metadata_index.debounce_seconds,
    )
