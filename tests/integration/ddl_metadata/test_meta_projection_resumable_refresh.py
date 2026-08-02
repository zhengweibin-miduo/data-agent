"""真实 MySQL 与 Elasticsearch 下的 Meta Projection 有界刷新和故障恢复。"""

from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.engine import CursorResult
from tests.helpers.checks import check_equal
from tests.helpers.factories import ensure_schema

from data_agent.data_sync.backfill import apply_buffered_event
from data_agent.data_sync.models import (
    BinlogCoordinate,
    DesiredColumn,
    DesiredSyncTable,
    RowOperation,
    SyncPhase,
    SyncRowEvent,
    primary_key_identity,
)
from data_agent.data_sync.repository import BufferedSyncEvent, ClaimedSyncTask
from data_agent.data_sync.tables import (
    data_sync_event,
    data_sync_key_owner,
    data_sync_task,
)
from data_agent.ddl_metadata.meta_projection.desired import enqueue_value_refresh
from data_agent.ddl_metadata.meta_projection.elasticsearch import (
    MetadataValueElasticsearchIndex,
)
from data_agent.ddl_metadata.meta_projection.models import (
    ClaimedMetadataIndexWork,
    MetadataIndexDesired,
    MetadataIndexOperation,
    MetadataIndexTarget,
    MetadataObjectKind,
    MetadataValueRefreshPhase,
)
from data_agent.ddl_metadata.meta_projection.repository import (
    MetadataIndexOutboxRepository,
)
from data_agent.ddl_metadata.meta_projection.tables import (
    metadata_index_outbox,
    metadata_value_frequency,
    metadata_value_publication,
)
from data_agent.ddl_metadata.meta_projection.value_refresh import (
    MetadataValueFrequencyRepository,
    MetadataValueRefresh,
    ValueRefreshPersistenceError,
)
from data_agent.ddl_metadata.persistence.tables import column_info, table_info
from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.settings import app_config


async def _phase(table_id: str) -> str | None:
    async with MySQLDatabase.session() as session:
        return await session.scalar(
            select(metadata_index_outbox.c.phase).where(
                metadata_index_outbox.c.target == MetadataIndexTarget.VALUES.value,
                metadata_index_outbox.c.object_id == table_id,
            )
        )


async def _dispatch_until(table_id: str, phase: MetadataValueRefreshPhase) -> int:
    """逐个有界工作单元推进到指定阶段。"""
    calls = 0
    while await _phase(table_id) != phase.value:
        calls += 1
        if calls > 40:
            raise AssertionError(f"字段值刷新未收敛到 {phase.value}")
        await _run_target_unit(table_id)
    return calls


async def _run_target_unit(table_id: str) -> None:
    """定向领取目标表，避免全套集成测试的其他 outbox 行影响执行顺序。"""
    lease_token = uuid4().hex
    async with MySQLDatabase.session() as session:
        await session.execute(
            update(metadata_index_outbox)
            .where(
                metadata_index_outbox.c.target
                == MetadataIndexTarget.VALUES.value,
                metadata_index_outbox.c.object_id == table_id,
            )
            .values(
                lease_token=lease_token,
                lease_expires_at=text("DATE_ADD(NOW(), INTERVAL 10 MINUTE)"),
            )
        )
        row = (
            (
                await session.execute(
                    select(metadata_index_outbox).where(
                        metadata_index_outbox.c.target
                        == MetadataIndexTarget.VALUES.value,
                        metadata_index_outbox.c.object_id == table_id,
                    )
                )
            )
            .mappings()
            .one()
        )
    item = ClaimedMetadataIndexWork(
        target=row["target"],
        object_kind=row["object_kind"],
        object_id=row["object_id"],
        operation=row["operation"],
        desired_version=row["desired_version"],
        frequency_version=row["frequency_version"],
        lease_token=lease_token,
        progress_column_id=row["progress_column_id"],
        phase=row["phase"],
        last_primary_key=row["last_primary_key"],
        bulk_cursor=row["bulk_cursor"],
        index_generation=row["index_generation"],
    )
    await MetadataValueRefresh().run_next_unit(item)


async def _documents(table_id: str) -> list[dict[str, object]]:
    client = ElasticsearchClient.get_client()
    await client.indices.refresh(index=app_config.elasticsearch.metadata_value_index)
    response = await client.search(
        index=app_config.elasticsearch.metadata_value_index,
        size=100,
        query={"term": {"table_id": table_id}},
    )
    return [hit["_source"] for hit in response["hits"]["hits"]]


async def _delete_test_documents(table_id: str) -> None:
    client = ElasticsearchClient.get_client()
    response = await client.search(
        index=app_config.elasticsearch.metadata_value_index,
        size=100,
        query={"term": {"table_id": table_id}},
        source=False,
    )
    operations = [
        {
            "delete": {
                "_index": app_config.elasticsearch.metadata_value_index,
                "_id": hit["_id"],
            }
        }
        for hit in response["hits"]["hits"]
    ]
    if operations:
        await client.bulk(operations=operations, refresh=True)


@pytest.mark.integration
async def test_value_refresh_is_bounded_and_recovers_publish_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨批扫描及 publish/cleanup 结算中断后必须继续收敛到精确 Top-N。"""
    suffix = uuid4().hex[:8]
    table_id = f"table-{suffix}"
    id_column_id = f"column-{suffix}-id"
    region_id = f"column-{suffix}-region"
    status_id = f"column-{suffix}-status"
    target_table = f"bounded_{suffix}"
    source = f"bounded-{suffix}"
    profile = {
        "decision": "index",
        "sensitivity": "non_sensitive",
        "reason": "集成测试字段允许索引",
        "evidence": [table_id],
    }
    skipped = {
        "decision": "skip",
        "sensitivity": "non_sensitive",
        "reason": "主键不建立字段值索引",
        "evidence": [table_id],
    }
    desired = DesiredSyncTable(
        source=source,
        source_schema="source_demo",
        source_table=target_table,
        target_table=target_table,
        columns=[
            DesiredColumn(
                id=id_column_id,
                name="id",
                data_type="BIGINT",
                nullable=False,
            ),
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
        primary_key=["id"],
        schema_fingerprint="s" * 64,
    )
    client = ElasticsearchClient.initialize()
    index_ready = False
    mysql_ready = False
    task_id: int | None = None
    try:
        await ensure_schema()
        mysql_ready = True
        await MetadataValueElasticsearchIndex(client).setup()
        index_ready = True
        async with MySQLDatabase.session() as session:
            await session.execute(
                text(
                    f"CREATE TABLE `{app_config.data_sync.dw_database}`."
                    f"`{target_table}` (id BIGINT NOT NULL PRIMARY KEY, "
                    "region VARCHAR(64) NOT NULL, status VARCHAR(64) NOT NULL)"
                )
            )
            await session.execute(
                text(
                    f"INSERT INTO `{app_config.data_sync.dw_database}`."
                    f"`{target_table}` (id, region, status) VALUES "
                    "(1, '华东', '启用'), (2, '华东', '启用'), (3, '华西', '停用')"
                )
            )
            await session.execute(
                insert(data_sync_key_owner),
                [
                    {
                        "target_table": target_table,
                        "primary_key_hash": key_hash,
                        "primary_key_json": document,
                        "source": source,
                        "deleted": False,
                    }
                    for document, key_hash in (
                        primary_key_identity(desired, {"id": row_id})
                        for row_id in (1, 2, 3)
                    )
                ],
            )
            await session.execute(
                insert(table_info).values(
                    id=table_id,
                    name=target_table,
                    role="dimension",
                    description="有界刷新集成测试表",
                    alias=[],
                )
            )
            await session.execute(
                insert(column_info),
                [
                    {
                        "id": column_id,
                        "name": name,
                        "type": data_type,
                        "role": "dimension",
                        "examples": [],
                        "description": name,
                        "alias": [],
                        "index_profile": index_profile
                        | {"evidence": [table_id, column_id]},
                        "table_id": table_id,
                    }
                    for column_id, name, data_type, index_profile in (
                        (id_column_id, "id", "BIGINT", skipped),
                        (region_id, "region", "VARCHAR(64)", profile),
                        (status_id, "status", "VARCHAR(64)", profile),
                    )
                ],
            )
            task_result = await session.execute(
                insert(data_sync_task).values(
                    source=source,
                    source_schema=desired.source_schema,
                    source_table=desired.source_table,
                    target_table=target_table,
                    desired_json=desired.model_dump(mode="json"),
                    desired_hash=desired.desired_hash(),
                    phase=SyncPhase.STREAMING.value,
                    lease_token="c" * 32,
                    lease_expires_at=text("DATE_ADD(NOW(), INTERVAL 10 MINUTE)"),
                )
            )
            task_primary_key = cast(
                CursorResult[object],
                task_result,
            ).inserted_primary_key
            assert task_primary_key is not None
            task_id = int(task_primary_key[0])
            await enqueue_value_refresh(session, desired, {"initial": True})

        cursor: dict[str, object] | None = None
        for _ in range(40):
            await _run_target_unit(table_id)
            async with MySQLDatabase.session() as session:
                cursor = await session.scalar(
                    select(metadata_index_outbox.c.last_primary_key).where(
                        metadata_index_outbox.c.object_id == table_id
                    )
                )
            if cursor is not None:
                break
        check_equal(
            "首个 SCAN 批次提交 schema 绑定主键游标",
            cursor,
            {
                "v": 1,
                "schema_fingerprint": desired.schema_fingerprint,
                "columns": ["id"],
                "types": ["BIGINT"],
                "values": [3],
            },
        )

        await _dispatch_until(table_id, MetadataValueRefreshPhase.PUBLISH)
        original_publish = MetadataValueFrequencyRepository.settle_publish
        publish_failed = False

        async def fail_publish_once(
            repository: MetadataValueFrequencyRepository,
            item: object,
            document_ids: object,
        ) -> None:
            nonlocal publish_failed
            if not publish_failed:
                publish_failed = True
                raise RuntimeError("simulated publish settle failure")
            await original_publish(repository, item, document_ids)  # type: ignore[arg-type]

        monkeypatch.setattr(
            MetadataValueFrequencyRepository,
            "settle_publish",
            fail_publish_once,
        )
        with pytest.raises(ValueRefreshPersistenceError):
            await _run_target_unit(table_id)
        monkeypatch.setattr(
            MetadataValueFrequencyRepository,
            "settle_publish",
            original_publish,
        )
        first_units = await _dispatch_until(
            table_id,
            MetadataValueRefreshPhase.COMPLETE,
        )
        check_equal("发布结算中断确实发生", publish_failed, True)
        check_equal("首次精确 Top-N", {
            (row["column_id"], row["value_keyword"], row["frequency"])
            for row in await _documents(table_id)
        }, {
            (region_id, "华东", 2),
            (region_id, "华西", 1),
            (status_id, "启用", 2),
            (status_id, "停用", 1),
        })
        assert first_units > 1

        assert task_id is not None
        task = ClaimedSyncTask(
            id=task_id,
            desired=desired,
            desired_hash=desired.desired_hash(),
            phase=SyncPhase.STREAMING,
            lease_token="c" * 32,
            attempts=0,
            snapshot=None,
            captured=None,
            applied=None,
            last_backfill_key=None,
        )

        async def apply_event(event: SyncRowEvent) -> BufferedSyncEvent:
            async with MySQLDatabase.session() as session:
                result = await session.execute(
                    insert(data_sync_event).values(
                        task_id=task_id,
                        source=event.source,
                        binlog_file=event.coordinate.file,
                        binlog_position=event.coordinate.position,
                        row_index=event.coordinate.row_index,
                        payload_json=event.model_dump(mode="json"),
                    )
                )
                event_primary_key = cast(
                    CursorResult[object],
                    result,
                ).inserted_primary_key
                assert event_primary_key is not None
                buffered = BufferedSyncEvent(
                    id=int(event_primary_key[0]),
                    event=event,
                )
                await apply_buffered_event(
                    session,
                    task,
                    buffered,
                    dw_database=app_config.data_sync.dw_database,
                )
                return buffered

        insert_event = SyncRowEvent(
            source=source,
            source_schema=desired.source_schema,
            source_table=desired.source_table,
            coordinate=BinlogCoordinate(
                file="mysql-bin.000001",
                position=100,
                row_index=0,
            ),
            operation=RowOperation.INSERT,
            after={"id": 4, "region": "华南", "status": "启用"},
        )
        duplicate = await apply_event(insert_event)
        async with MySQLDatabase.session() as session:
            frequency_version = await session.scalar(
                select(metadata_index_outbox.c.frequency_version).where(
                    metadata_index_outbox.c.object_id == table_id
                )
            )
            inserted_frequency = {
                (row.value_text, int(row.frequency))
                for row in await session.execute(
                    select(
                        metadata_value_frequency.c.value_text,
                        metadata_value_frequency.c.frequency,
                    ).where(
                        metadata_value_frequency.c.table_id == table_id,
                        metadata_value_frequency.c.frequency_version
                        == frequency_version,
                    )
                )
            }
            desired_version_before_failure = await session.scalar(
                select(metadata_index_outbox.c.desired_version).where(
                    metadata_index_outbox.c.object_id == table_id
                )
            )
        check_equal("CDC INSERT 精确加一", inserted_frequency, {
            ("华东", 2),
            ("华西", 1),
            ("华南", 1),
            ("启用", 3),
            ("停用", 1),
        })
        failed_event = SyncRowEvent(
            source=source,
            source_schema=desired.source_schema,
            source_table=desired.source_table,
            coordinate=BinlogCoordinate(
                file="mysql-bin.000001",
                position=105,
                row_index=0,
            ),
            operation=RowOperation.INSERT,
            after={"id": 5, "region": "华北", "status": "停用"},
        )
        with pytest.raises(RuntimeError, match="事件确认失败"):
            async with MySQLDatabase.session() as session:
                await apply_buffered_event(
                    session,
                    task,
                    BufferedSyncEvent(id=duplicate.id, event=failed_event),
                    dw_database=app_config.data_sync.dw_database,
                )
        async with MySQLDatabase.session() as session:
            failed_row_count = await session.scalar(
                text(
                    f"SELECT COUNT(*) FROM `{app_config.data_sync.dw_database}`."
                    f"`{target_table}` WHERE id = 5"
                )
            )
            frequency_after_failure = {
                (row.value_text, int(row.frequency))
                for row in await session.execute(
                    select(
                        metadata_value_frequency.c.value_text,
                        metadata_value_frequency.c.frequency,
                    ).where(
                        metadata_value_frequency.c.table_id == table_id,
                        metadata_value_frequency.c.frequency_version
                        == frequency_version,
                    )
                )
            }
            desired_version_after_failure = await session.scalar(
                select(metadata_index_outbox.c.desired_version).where(
                    metadata_index_outbox.c.object_id == table_id
                )
            )
        check_equal("事件确认失败回滚 DW DML", failed_row_count, 0)
        check_equal(
            "事件确认失败回滚频次差量",
            frequency_after_failure,
            inserted_frequency,
        )
        check_equal(
            "事件确认失败回滚 refresh desired",
            desired_version_after_failure,
            desired_version_before_failure,
        )

        update_event = SyncRowEvent(
            source=source,
            source_schema=desired.source_schema,
            source_table=desired.source_table,
            coordinate=BinlogCoordinate(
                file="mysql-bin.000001",
                position=110,
                row_index=0,
            ),
            operation=RowOperation.UPDATE,
            before={"id": 4, "region": "华南", "status": "启用"},
            after={"id": 4, "region": "华北", "status": "停用"},
        )
        await apply_event(update_event)
        delete_event = SyncRowEvent(
            source=source,
            source_schema=desired.source_schema,
            source_table=desired.source_table,
            coordinate=BinlogCoordinate(
                file="mysql-bin.000001",
                position=120,
                row_index=0,
            ),
            operation=RowOperation.DELETE,
            before={"id": 4, "region": "华北", "status": "停用"},
        )
        await apply_event(delete_event)
        await _dispatch_until(table_id, MetadataValueRefreshPhase.COMPLETE)
        check_equal("CDC UPDATE/DELETE 与重复事件最终精确", {
            (row["column_id"], row["value_keyword"], row["frequency"])
            for row in await _documents(table_id)
        }, {
            (region_id, "华东", 2),
            (region_id, "华西", 1),
            (status_id, "启用", 2),
            (status_id, "停用", 1),
        })

        async with MySQLDatabase.session() as session:
            await session.execute(
                text(
                    f"DELETE FROM `{app_config.data_sync.dw_database}`."
                    f"`{target_table}` WHERE id = 3"
                )
            )
            await MetadataIndexOutboxRepository(session).enqueue(
                [
                    MetadataIndexDesired(
                        target=MetadataIndexTarget.VALUES,
                        object_kind=MetadataObjectKind.TABLE,
                        object_id=table_id,
                        operation=MetadataIndexOperation.REFRESH,
                        desired_version="2" * 64,
                        frequency_version="b" * 64,
                    )
                ]
            )

        await _dispatch_until(table_id, MetadataValueRefreshPhase.CLEANUP)
        original_cleanup = MetadataValueFrequencyRepository.settle_cleanup
        cleanup_failed = False

        async def fail_cleanup_once(
            repository: MetadataValueFrequencyRepository,
            item: object,
            document_ids: object,
        ) -> None:
            nonlocal cleanup_failed
            if not cleanup_failed:
                cleanup_failed = True
                raise RuntimeError("simulated cleanup settle failure")
            await original_cleanup(repository, item, document_ids)  # type: ignore[arg-type]

        monkeypatch.setattr(
            MetadataValueFrequencyRepository,
            "settle_cleanup",
            fail_cleanup_once,
        )
        with pytest.raises(ValueRefreshPersistenceError):
            await _run_target_unit(table_id)
        monkeypatch.setattr(
            MetadataValueFrequencyRepository,
            "settle_cleanup",
            original_cleanup,
        )
        await _dispatch_until(table_id, MetadataValueRefreshPhase.COMPLETE)
        check_equal("清理结算中断确实发生", cleanup_failed, True)
        check_equal("最终索引只保留新精确集合", {
            (row["column_id"], row["value_keyword"], row["frequency"])
            for row in await _documents(table_id)
        }, {
            (region_id, "华东", 2),
            (status_id, "启用", 2),
        })
        async with MySQLDatabase.session() as session:
            remaining_frequency_versions = set(
                (
                    await session.execute(
                        select(metadata_value_frequency.c.frequency_version)
                        .where(metadata_value_frequency.c.table_id == table_id)
                        .distinct()
                    )
                ).scalars()
            )
        check_equal(
            "完成前回收旧频次代次",
            remaining_frequency_versions,
            {"b" * 64},
        )
    finally:
        if index_ready:
            await _delete_test_documents(table_id)
        if mysql_ready:
            async with MySQLDatabase.session() as session:
                if task_id is not None:
                    await session.execute(
                        delete(data_sync_event).where(
                            data_sync_event.c.task_id == task_id
                        )
                    )
                await session.execute(
                    delete(data_sync_key_owner).where(
                        data_sync_key_owner.c.target_table == target_table
                    )
                )
                await session.execute(
                    delete(metadata_value_publication).where(
                        metadata_value_publication.c.table_id == table_id
                    )
                )
                await session.execute(
                    delete(metadata_value_frequency).where(
                        metadata_value_frequency.c.table_id == table_id
                    )
                )
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
                        f"`{app_config.data_sync.dw_database}`.`{target_table}`"
                    )
                )
        await ElasticsearchClient.close()
        await MySQLDatabase.close()
