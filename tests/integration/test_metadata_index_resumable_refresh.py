"""真实 MySQL 与 Elasticsearch 下的字段值刷新恢复检查。"""

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, select, text

from data_agent.data_sync.models import DesiredColumn, DesiredSyncTable, SyncPhase
from data_agent.data_sync.tables import data_sync_task
from data_agent.ddl_metadata.persistence.tables import column_info, table_info
from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.metadata_indexing.dispatcher import MetadataIndexDispatcher
from data_agent.metadata_indexing.elasticsearch import MetadataValueElasticsearchIndex
from data_agent.metadata_indexing.models import (
    MetadataIndexDesired,
    MetadataIndexOperation,
    MetadataIndexTarget,
    MetadataObjectKind,
    MetadataValueProjection,
)
from data_agent.metadata_indexing.repository import MetadataIndexOutboxRepository
from data_agent.metadata_indexing.tables import metadata_index_outbox
from data_agent.settings import app_config
from tests.helpers.checks import check_equal
from tests.helpers.factories import ensure_schema


@pytest.mark.integration
async def test_value_refresh_resumes_across_claims_and_finalizes() -> None:
    """字段游标必须跨领取恢复，末字段落盘后的领取才清理旧版本。"""
    suffix = uuid4().hex[:8]
    table_id = f"table-{suffix}"
    region_id = f"column-{suffix}-region"
    status_id = f"column-{suffix}-status"
    target_table = f"resumable_{suffix}"
    source = f"resumable-{suffix}"
    refresh_version = "n" * 64
    old_version = "o" * 64
    profile = {
        "decision": "index",
        "sensitivity": "non_sensitive",
        "reason": "集成测试字段允许索引",
        "evidence": [table_id],
    }
    desired = DesiredSyncTable(
        source=source,
        source_schema="source_demo",
        source_table=target_table,
        target_table=target_table,
        columns=[
            DesiredColumn(
                id=region_id,
                name="region",
                data_type="VARCHAR(64)",
                nullable=False,
            ),
            DesiredColumn(
                id=status_id,
                name="status",
                data_type="VARCHAR(64)",
                nullable=False,
            ),
        ],
        primary_key=["region"],
        schema_fingerprint="s" * 64,
    )
    client = ElasticsearchClient.initialize()
    value_index = MetadataValueElasticsearchIndex(client)
    mysql_ready = False
    index_ready = False

    async def old_projection() -> AsyncIterator[MetadataValueProjection]:
        """产生一个只属于旧刷新代次的淘汰值。"""
        yield MetadataValueProjection(
            column_id=region_id,
            table_id=table_id,
            value_text="legacy",
            value_keyword="legacy",
            frequency=1,
            refresh_version=old_version,
            schema_fingerprint=desired.schema_fingerprint,
        )

    try:
        await ensure_schema()
        mysql_ready = True
        await value_index.setup()
        index_ready = True
        async with MySQLDatabase.session() as session:
            await session.execute(
                text(
                    f"CREATE TABLE `{app_config.data_sync.dw_database}`."
                    f"`{target_table}` (region VARCHAR(64) NOT NULL, "
                    "status VARCHAR(64) NOT NULL)"
                )
            )
            await session.execute(
                text(
                    f"INSERT INTO `{app_config.data_sync.dw_database}`."
                    f"`{target_table}` (region, status) VALUES "
                    "('华东', '启用'), ('华东', '启用'), ('华西', '停用')"
                )
            )
            await session.execute(
                insert(table_info).values(
                    id=table_id,
                    name=target_table,
                    role="dimension",
                    description="可恢复刷新集成测试表",
                    alias=[],
                )
            )
            await session.execute(
                insert(column_info),
                [
                    {
                        "id": column_id,
                        "name": name,
                        "type": "VARCHAR(64)",
                        "role": "dimension",
                        "examples": [],
                        "description": name,
                        "alias": [],
                        "index_profile": profile | {"evidence": [table_id, column_id]},
                        "table_id": table_id,
                    }
                    for column_id, name in (
                        (region_id, "region"),
                        (status_id, "status"),
                    )
                ],
            )
            await session.execute(
                insert(data_sync_task).values(
                    source=source,
                    source_schema=desired.source_schema,
                    source_table=desired.source_table,
                    target_table=target_table,
                    desired_json=desired.model_dump(mode="json"),
                    desired_hash=desired.desired_hash(),
                    phase=SyncPhase.STREAMING.value,
                )
            )
            await MetadataIndexOutboxRepository(session).enqueue(
                [
                    MetadataIndexDesired(
                        target=MetadataIndexTarget.VALUES,
                        object_kind=MetadataObjectKind.TABLE,
                        object_id=table_id,
                        operation=MetadataIndexOperation.REFRESH,
                        desired_version=refresh_version,
                    )
                ]
            )
        await value_index.upsert_projections(old_projection())
        await client.indices.refresh(
            index=app_config.elasticsearch.metadata_value_index
        )

        first = await MetadataIndexDispatcher().dispatch()
        async with MySQLDatabase.session() as session:
            first_progress = await session.scalar(
                select(metadata_index_outbox.c.progress_column_id).where(
                    metadata_index_outbox.c.object_id == table_id
                )
            )
        old_after_first = await client.count(
            index=app_config.elasticsearch.metadata_value_index,
            query={
                "bool": {
                    "filter": [
                        {"term": {"table_id": table_id}},
                        {"term": {"refresh_version": old_version}},
                    ]
                }
            },
        )

        second = await MetadataIndexDispatcher().dispatch()
        async with MySQLDatabase.session() as session:
            second_progress = await session.scalar(
                select(metadata_index_outbox.c.progress_column_id).where(
                    metadata_index_outbox.c.object_id == table_id
                )
            )
        third = await MetadataIndexDispatcher().dispatch()
        async with MySQLDatabase.session() as session:
            pending = await session.scalar(
                select(metadata_index_outbox.c.object_id).where(
                    metadata_index_outbox.c.object_id == table_id
                )
            )
        final = await client.search(
            index=app_config.elasticsearch.metadata_value_index,
            size=10,
            query={"term": {"table_id": table_id}},
        )

        check_equal("前两次领取只推进字段", [first, second], [0, 0])
        check_equal("首次领取持久化首字段", first_progress, region_id)
        check_equal("部分完成不清理旧版本", old_after_first["count"], 1)
        check_equal("第二次领取持久化末字段", second_progress, status_id)
        check_equal("第三次领取完成最终清理", third, 1)
        check_equal("最终确认删除 outbox", pending, None)
        sources = [hit["_source"] for hit in final["hits"]["hits"]]
        check_equal(
            "最终只保留当前刷新代次",
            {row["refresh_version"] for row in sources},
            {refresh_version},
        )
        check_equal(
            "最终索引收敛到两个字段的当前 Top-N",
            {(row["column_id"], row["value_keyword"]) for row in sources},
            {
                (region_id, "华东"),
                (region_id, "华西"),
                (status_id, "启用"),
                (status_id, "停用"),
            },
        )
    finally:
        try:
            if mysql_ready:
                async with MySQLDatabase.session() as session:
                    await session.execute(
                        delete(metadata_index_outbox).where(
                            metadata_index_outbox.c.object_id == table_id
                        )
                    )
                    await session.execute(
                        delete(data_sync_task).where(data_sync_task.c.source == source)
                    )
                    await session.execute(
                        delete(column_info).where(column_info.c.table_id == table_id)
                    )
                    await session.execute(
                        delete(table_info).where(table_info.c.id == table_id)
                    )
                    await session.execute(
                        text(
                            "DROP TABLE IF EXISTS "
                            f"`{app_config.data_sync.dw_database}`."
                            f"`{target_table}`"
                        )
                    )
        finally:
            try:
                if index_ready:
                    await client.delete_by_query(
                        index=app_config.elasticsearch.metadata_value_index,
                        conflicts="proceed",
                        refresh=True,
                        query={"term": {"table_id": table_id}},
                    )
            finally:
                await ElasticsearchClient.close()
                await MySQLDatabase.close()
