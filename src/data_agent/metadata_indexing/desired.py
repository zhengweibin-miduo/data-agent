"""Meta 与 DW 事务写入的索引期望状态。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.data_sync.models import DesiredSyncTable
from data_agent.data_sync.tables import data_sync_task
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
    physical_tables = {table.id: table for table in schema.tables}
    physical_columns = {
        column.id: column for table in schema.tables for column in table.columns
    }
    table_by_column = {
        column.id: table for table in schema.tables for column in table.columns
    }
    objects: list[tuple[MetadataObjectKind, str, object]] = []
    for table in schema.tables:
        objects.append(
            (
                MetadataObjectKind.TABLE,
                table.id,
                {
                    "physical": table.model_dump(mode="json"),
                    "semantic": semantic_tables[table.id].model_dump(mode="json"),
                    "columns": [
                        {
                            "physical": column.model_dump(mode="json"),
                            "semantic": semantic_columns[column.id].model_dump(
                                mode="json"
                            ),
                        }
                        for column in table.columns
                    ],
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
                    "table_semantic": semantic_tables[table.id].model_dump(mode="json"),
                    "table_physical": table.model_dump(mode="json"),
                },
            )
            for column in table.columns
        )
    for metric in metrics:
        related = []
        for column_id in sorted(metric.relevant_column_ids):
            column = physical_columns[column_id]
            table = physical_tables[table_by_column[column_id].id]
            related.append(
                {
                    "physical": column.model_dump(mode="json"),
                    "semantic": semantic_columns[column_id].model_dump(mode="json"),
                    "table_physical": table.model_dump(mode="json"),
                    "table_semantic": semantic_tables[table.id].model_dump(mode="json"),
                }
            )
        objects.append(
            (
                MetadataObjectKind.METRIC,
                metric.id,
                {"metric": metric.model_dump(mode="json"), "related": related},
            )
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
    eligibility_by_table = {
        table.id: [
            {
                "column_id": column.id,
                "index_profile": semantic_columns[column.id].value_index.model_dump(
                    mode="json"
                ),
            }
            for column in table.columns
        ]
        for table in schema.tables
    }
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
                    "field_eligibility": eligibility_by_table[table.id],
                    "projection_version": app_config.metadata_index.projection_version,
                }
            ),
        )
        for table in schema.tables
    )
    return semantic


async def shared_value_refresh_states(
    session: AsyncSession,
    target_tables: set[str],
) -> list[MetadataIndexDesired]:
    """为资格变化涉及的共享 DW 目标生成全部表级刷新状态。"""
    if not target_tables:
        return []
    peer_payloads = (
        await session.scalars(
            select(data_sync_task.c.desired_json).where(
                data_sync_task.c.target_table.in_(target_tables)
            )
        )
    ).all()
    peers = [DesiredSyncTable.model_validate(payload) for payload in peer_payloads]
    column_ids = {column.id for peer in peers for column in peer.columns}
    rows = (
        await session.execute(
            select(
                column_info.c.id,
                column_info.c.table_id,
                column_info.c.index_profile,
            ).where(column_info.c.id.in_(column_ids))
        )
    ).mappings()
    column_metadata = {
        str(row["id"]): {
            "table_id": str(row["table_id"]),
            "index_profile": row["index_profile"],
        }
        for row in rows
    }
    states: list[MetadataIndexDesired] = []
    for target_table in sorted(target_tables):
        target_peers = [peer for peer in peers if peer.target_table == target_table]
        eligibility = sorted(
            [
                {
                    "column_id": column.id,
                    "name": column.name,
                    "index_profile": column_metadata[column.id]["index_profile"],
                }
                for peer in target_peers
                for column in peer.columns
                if column.id in column_metadata
            ],
            key=lambda item: str(item["column_id"]),
        )
        table_ids = {
            str(column_metadata[column.id]["table_id"])
            for peer in target_peers
            for column in peer.columns
            if column.id in column_metadata
        }
        version = metadata_desired_version(
            {
                "target_table": target_table,
                "peer_generations": sorted(
                    peer.desired_hash() for peer in target_peers
                ),
                "field_eligibility": eligibility,
                "projection_version": app_config.metadata_index.projection_version,
            }
        )
        states.extend(
            MetadataIndexDesired(
                target=MetadataIndexTarget.VALUES,
                object_kind=MetadataObjectKind.TABLE,
                object_id=table_id,
                operation=MetadataIndexOperation.REFRESH,
                desired_version=version,
            )
            for table_id in sorted(table_ids)
        )
    return states


async def enqueue_value_refresh(
    session: AsyncSession,
    desired: DesiredSyncTable,
    version_payload: object,
) -> None:
    """在当前 DW 事务中合并一条表级字段值刷新。"""
    peer_payloads = (
        await session.scalars(
            select(data_sync_task.c.desired_json).where(
                data_sync_task.c.target_table == desired.target_table
            )
        )
    ).all()
    peers = [DesiredSyncTable.model_validate(payload) for payload in peer_payloads]
    column_ids = {column.id for item in [desired, *peers] for column in item.columns}
    table_identifiers = set(
        await session.scalars(
            select(column_info.c.table_id).where(column_info.c.id.in_(column_ids))
        )
    )
    if not table_identifiers:
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
                        "target_table": desired.target_table,
                    }
                ),
            )
            for table_identifier in sorted(table_identifiers)
        ],
        debounce_seconds=app_config.metadata_index.debounce_seconds,
    )
