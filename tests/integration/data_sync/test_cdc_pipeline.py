"""真实 MySQL 上的 DW 回填与 Binlog 收敛检查。"""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import func, make_url, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from tests.helpers.checks import check_condition, check_equal
from tests.helpers.factories import cleanup_schema, ensure_schema, semantic_for

from data_agent.data_sync.application.contracts import ClaimedSyncTask
from data_agent.data_sync.backfill import (
    apply_backfill_batch,
    apply_buffered_event,
    read_backfill_batch,
)
from data_agent.data_sync.binlog import MySQLSourceClient
from data_agent.data_sync.models import (
    BinlogCoordinate,
    DesiredColumn,
    DesiredSyncTable,
    RowOperation,
    SyncPhase,
    SyncRowEvent,
    build_desired_tables,
)
from data_agent.data_sync.repository import DataSyncRepository
from data_agent.data_sync.schema_sync import DWSchemaSynchronizer
from data_agent.data_sync.tables import data_sync_task
from data_agent.ddl_metadata.meta_projection.adapters.mysql_value_input import (
    MySQLValueProjectionParticipant,
)
from data_agent.ddl_metadata.parsing import parse_ddl
from data_agent.ddl_metadata.persistence.metadata_repository import MetadataRepository
from data_agent.errors import DataAgentError
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.settings import app_config


@pytest.mark.integration
async def test_backfill_then_binlog_converges() -> None:
    """分块历史行与后续写改删事件最终收敛到同一 DW 表。"""
    table_name = f"sync_fact_{uuid4().hex[:12]}"
    source_name = f"cdc_backfill_{uuid4().hex}"
    source_settings = app_config.data_sync.sources["source_demo"]
    schema = await parse_ddl(
        source_name,
        f"CREATE TABLE {table_name} "
        "(id BIGINT PRIMARY KEY, amount INT NOT NULL) ENGINE=InnoDB",
    )
    semantic = semantic_for(schema, fact=True)
    desired = build_desired_tables(
        schema,
        semantic,
        [],
        default_source_schema="source_demo",
    )[0]
    source = MySQLSourceClient(
        source_name,
        source_settings,
        connect_timeout_seconds=5,
        read_timeout_seconds=5,
    )
    source_writer = create_async_engine(
        make_url(app_config.mysql.url).set(database="source_demo")
    )
    try:
        await ensure_schema()
        # 步骤一：创建本测试独占源表并在记录 Binlog 基线前写入历史行。
        async with source_writer.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE TABLE source_demo.{table_name} "
                    "(id BIGINT PRIMARY KEY, amount INT NOT NULL) ENGINE=InnoDB"
                )
            )
            await connection.execute(
                text(
                    f"INSERT INTO source_demo.{table_name} (id, amount) VALUES (1, 10)"
                )
            )
        await source.check_capabilities()
        baseline = await source.current_coordinate()

        # 步骤二：按生产顺序原子提交 Meta 与 desired state，再领取任务并回填。
        async with MySQLDatabase.session() as session:
            await MetadataRepository(session).synchronize(schema, semantic, [])
            repository = DataSyncRepository(session)
            await repository.upsert_desired([desired])
            task = await _lease_scoped_task(session, desired)
        check_equal(
            "租约绑定 UUID 目标任务",
            (task.desired.source, task.desired.target_table),
            (source_name, table_name),
        )
        async with MySQLDatabase.session() as session:
            await DWSchemaSynchronizer(
                session,
                database=app_config.data_sync.dw_database,
            ).synchronize(desired)
            repository = DataSyncRepository(session)
            await repository.record_snapshot(task, baseline)
            await repository.advance_captured_coordinate(task, baseline)
            meta_table_ids = (
                await session.scalars(
                    text(
                        "SELECT table_id FROM column_info "
                        "WHERE table_id=:table_id ORDER BY id"
                    ),
                    {"table_id": schema.tables[0].id},
                )
            ).all()
            check_equal(
                "回填前字段均映射到当前 Meta 表",
                meta_table_ids,
                [schema.tables[0].id] * len(desired.columns),
            )
        rows = await read_backfill_batch(
            source.engine,
            desired,
            after_key=None,
            limit=10,
        )
        async with MySQLDatabase.session() as session:
            await apply_backfill_batch(
                session,
                task,
                rows,
                dw_database=app_config.data_sync.dw_database,
                value_projection=MySQLValueProjectionParticipant(session, task.desired),
            )

        # 步骤三：源表产生更新、插入和硬删除，再从基线捕获并顺序回放。
        async with source_writer.begin() as connection:
            await connection.execute(
                text(f"UPDATE source_demo.{table_name} SET amount=11 WHERE id=1")
            )
            await connection.execute(
                text(
                    f"INSERT INTO source_demo.{table_name} (id, amount) VALUES (2, 20)"
                )
            )
            await connection.execute(
                text(f"DELETE FROM source_demo.{table_name} WHERE id=1")
            )
        captured = await source.capture(
            source_schema="source_demo",
            source_table=table_name,
            start=baseline,
            limit=100,
        )
        check_condition(
            "捕获更新插入删除事件",
            len(captured.events) >= 3,
            actual=len(captured.events),
            expected="至少 3 个 ROW 事件",
        )
        async with MySQLDatabase.session() as session:
            repository = DataSyncRepository(session)
            for event in captured.events:
                await repository.append_event(task.id, event)
            await repository.advance_captured_coordinate(task, captured.tail)
        while True:
            async with MySQLDatabase.session() as session:
                pending = await DataSyncRepository(session).read_events(
                    task.id,
                    limit=1,
                )
            if not pending:
                break
            async with MySQLDatabase.session() as session:
                await apply_buffered_event(
                    session,
                    task,
                    pending[0],
                    dw_database=app_config.data_sync.dw_database,
                    value_projection=MySQLValueProjectionParticipant(
                        session,
                        task.desired,
                    ),
                )

        # 步骤四：DW 仅保留插入后的第二行，证明回填与硬删除均已收敛。
        async with MySQLDatabase.session() as session:
            result = await session.execute(
                text(f"SELECT id, amount FROM dw.{table_name} ORDER BY id")
            )
            check_equal("DW 最终业务行", [tuple(row) for row in result], [(2, 20)])
            coordinate = (
                await session.execute(
                    text(
                        "SELECT captured_file, captured_position, captured_row_index, "
                        "applied_file, applied_position, applied_row_index "
                        "FROM data_sync.data_sync_task WHERE id=:task_id"
                    ),
                    {"task_id": task.id},
                )
            ).mappings().one()
            check_equal(
                "捕获位点推进到安全事务边界",
                tuple(coordinate.values())[:3],
                (
                    captured.tail.file,
                    captured.tail.position,
                    captured.tail.row_index,
                ),
            )
            last_event_coordinate = captured.events[-1].coordinate
            check_equal(
                "应用位点推进到最后一条行事件",
                tuple(coordinate.values())[3:],
                (
                    last_event_coordinate.file,
                    last_event_coordinate.position,
                    last_event_coordinate.row_index,
                ),
            )

        # 步骤五：其他来源碰撞同一主键时，冲突先于 DW 写入发生。
        contender = replace(
            task,
            desired=desired.model_copy(update={"source": "source_other"}),
        )
        with pytest.raises(
            DataAgentError,
            match="DW 目标主键已由其他数据源占用",
        ) as error:
            async with MySQLDatabase.session() as session:
                await apply_backfill_batch(
                    session,
                    contender,
                    [{"id": 2, "amount": 999}],
                    dw_database=app_config.data_sync.dw_database,
                    value_projection=MySQLValueProjectionParticipant(
                        session, contender.desired
                    ),
                )
        check_equal("跨来源冲突错误码", error.value.code, "dw_primary_key_conflict")
        async with MySQLDatabase.session() as session:
            amount = await session.scalar(text(f"SELECT amount FROM dw.{table_name}"))
            check_equal("冲突不覆盖 DW 行", amount, 20)

        # 步骤六：跨越 Binlog 文件编号宽度时仍按数字顺序回放。
        async with MySQLDatabase.session() as session:
            repository = DataSyncRepository(session)
            for file_name in ("mysql-bin.1000000", "mysql-bin.999999"):
                await repository.append_event(
                    task.id,
                    SyncRowEvent(
                        source=source_name,
                        source_schema="source_demo",
                        source_table=table_name,
                        coordinate=BinlogCoordinate(
                            file=file_name,
                            position=4,
                            row_index=0,
                        ),
                        operation=RowOperation.INSERT,
                        after={"id": 3, "amount": 30},
                    ),
                )
        async with MySQLDatabase.session() as session:
            pending = await DataSyncRepository(session).read_events(task.id, limit=2)
            check_equal(
                "Binlog 文件按编号顺序回放",
                [item.event.coordinate.file for item in pending],
                ["mysql-bin.999999", "mysql-bin.1000000"],
            )

        # 步骤七：失败预算耗尽后释放租约并进入死信。
        async with MySQLDatabase.session() as session:
            phase = await DataSyncRepository(session).retry_failure(
                task,
                error_type="source_transport_error",
                retry_base_seconds=0,
                retry_max_seconds=0,
                max_attempts=1,
            )
            check_equal("失败预算耗尽进入死信", phase, SyncPhase.DEAD)
    finally:
        # 步骤八：只清理本测试 UUID 命名的源表、目标表、控制状态与 Meta。
        try:
            async with source_writer.begin() as connection:
                await connection.execute(
                    text(f"DROP TABLE IF EXISTS source_demo.{table_name}")
                )
            async with MySQLDatabase.session() as session:
                task_scope = {"source": source_name, "target_table": table_name}
                await session.execute(
                    text(
                        "DELETE FROM data_sync.data_sync_event WHERE task_id IN "
                        "(SELECT id FROM data_sync.data_sync_task "
                        "WHERE source=:source AND target_table=:target_table)"
                    ),
                    task_scope,
                )
                await session.execute(
                    text(
                        "DELETE FROM data_sync.data_sync_task "
                        "WHERE source=:source AND target_table=:target_table"
                    ),
                    task_scope,
                )
                await session.execute(
                    text(
                        "DELETE FROM data_sync.data_sync_key_owner "
                        "WHERE target_table=:target_table"
                    ),
                    {"target_table": table_name},
                )
                await session.execute(text(f"DROP TABLE IF EXISTS dw.{table_name}"))
            await cleanup_schema(schema)
        finally:
            await source.close()
            await source_writer.dispose()
            await MySQLDatabase.close()


@pytest.mark.integration
async def test_json_sql_null_and_literal_null_remain_distinct_after_cdc() -> None:
    """JSON SQL NULL 与 literal null 经真实 FULL ROW CDC 后保持不同语义。"""
    table_name = f"sync_json_null_{uuid4().hex}"
    source_name = f"cdc_json_null_{uuid4().hex}"
    source_settings = app_config.data_sync.sources["source_demo"]
    schema = await parse_ddl(
        source_name,
        f"CREATE TABLE {table_name} "
        "(id BIGINT PRIMARY KEY, payload JSON NULL) ENGINE=InnoDB",
    )
    semantic = semantic_for(schema, fact=False)
    desired = build_desired_tables(
        schema,
        semantic,
        [],
        default_source_schema="source_demo",
    )[0]
    desired = desired.model_copy(
        update={
            "columns": [
                column.model_copy(update={"nullable": True})
                if column.name == "payload"
                else column
                for column in desired.columns
            ]
        }
    )
    source = MySQLSourceClient(
        source_name,
        source_settings,
        connect_timeout_seconds=5,
        read_timeout_seconds=5,
    )
    source_writer = create_async_engine(
        make_url(app_config.mysql.url).set(database="source_demo")
    )
    try:
        await ensure_schema()
        # 步骤一：创建 UUID 独占 JSON 源表，并确认源库满足 ROW/FULL 捕获契约。
        async with source_writer.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE TABLE source_demo.{table_name} "
                    "(id BIGINT PRIMARY KEY, payload JSON NULL) ENGINE=InnoDB"
                )
            )
        await source.check_capabilities()
        baseline = await source.current_coordinate()

        # 步骤二：按生产顺序原子提交 Meta 与 desired state，再领取本测试任务。
        async with MySQLDatabase.session() as session:
            await MetadataRepository(session).synchronize(schema, semantic, [])
            repository = DataSyncRepository(session)
            await repository.upsert_desired([desired])
            task = await _lease_scoped_task(session, desired)
        check_equal(
            "租约绑定 UUID 目标任务",
            (task.desired.source, task.desired.target_table),
            (source_name, table_name),
        )
        async with MySQLDatabase.session() as session:
            await DWSchemaSynchronizer(
                session,
                database=app_config.data_sync.dw_database,
            ).synchronize(desired)
            repository = DataSyncRepository(session)
            await repository.record_snapshot(task, baseline)
            await repository.advance_captured_coordinate(task, baseline)
            meta_table_ids = (
                await session.scalars(
                    text(
                        "SELECT table_id FROM column_info "
                        "WHERE table_id=:table_id ORDER BY id"
                    ),
                    {"table_id": schema.tables[0].id},
                )
            ).all()
            check_equal(
                "CDC 前字段均映射到当前 Meta 表",
                meta_table_ids,
                [schema.tables[0].id] * len(desired.columns),
            )

        # 步骤三：分别写入 SQL NULL 与 JSON literal null，再从基线捕获 FULL ROW。
        async with source_writer.begin() as connection:
            await connection.execute(
                text(
                    f"INSERT INTO source_demo.{table_name} (id, payload) VALUES "
                    "(1, NULL), (2, CAST('null' AS JSON))"
                )
            )
        captured = await source.capture(
            source_schema="source_demo",
            source_table=table_name,
            start=baseline,
            limit=10,
        )
        check_equal("捕获两条 JSON 空值 ROW 事件", len(captured.events), 2)
        check_equal(
            "捕获层区分两种 JSON 空值",
            [event.after for event in captured.events],
            [
                {"id": 1, "payload": None},
                {"id": 2, "payload": {"$json": "null"}},
            ],
        )

        # 步骤四：先写入 durable event，再仅从持久化事件读取并逐条应用到 DW。
        async with MySQLDatabase.session() as session:
            repository = DataSyncRepository(session)
            for event in captured.events:
                await repository.append_event(task.id, event)
            await repository.advance_captured_coordinate(task, captured.tail)
        while True:
            async with MySQLDatabase.session() as session:
                pending = await DataSyncRepository(session).read_events(
                    task.id,
                    limit=1,
                )
            if not pending:
                break
            async with MySQLDatabase.session() as session:
                await apply_buffered_event(
                    session,
                    task,
                    pending[0],
                    dw_database=app_config.data_sync.dw_database,
                    value_projection=MySQLValueProjectionParticipant(
                        session,
                        task.desired,
                    ),
                )

        # 步骤五：IS NULL 只匹配 SQL NULL，JSON_TYPE 则识别 literal null。
        async with MySQLDatabase.session() as session:
            result = await session.execute(
                text(
                    f"SELECT id, payload IS NULL AS is_sql_null, "
                    f"JSON_TYPE(payload) AS json_type FROM dw.{table_name} ORDER BY id"
                )
            )
            check_equal(
                "DW 区分 SQL NULL 与 JSON literal null",
                [tuple(row) for row in result],
                [(1, 1, None), (2, 0, "NULL")],
            )
    finally:
        # 步骤六：严格按本测试 UUID source 与表名清理源、DW、控制状态和 Meta。
        try:
            async with source_writer.begin() as connection:
                await connection.execute(
                    text(f"DROP TABLE IF EXISTS source_demo.{table_name}")
                )
            async with MySQLDatabase.session() as session:
                task_scope = {"source": source_name, "target_table": table_name}
                await session.execute(
                    text(
                        "DELETE FROM data_sync.data_sync_event WHERE task_id IN "
                        "(SELECT id FROM data_sync.data_sync_task "
                        "WHERE source=:source AND target_table=:target_table)"
                    ),
                    task_scope,
                )
                await session.execute(
                    text(
                        "DELETE FROM data_sync.data_sync_task "
                        "WHERE source=:source AND target_table=:target_table"
                    ),
                    task_scope,
                )
                await session.execute(
                    text(
                        "DELETE FROM data_sync.data_sync_key_owner "
                        "WHERE target_table=:target_table"
                    ),
                    {"target_table": table_name},
                )
                await session.execute(text(f"DROP TABLE IF EXISTS dw.{table_name}"))
            await cleanup_schema(schema)
        finally:
            await source.close()
            await source_writer.dispose()
            await MySQLDatabase.close()


async def _lease_scoped_task(
    session: AsyncSession,
    desired: DesiredSyncTable,
) -> ClaimedSyncTask:
    """只给本测试唯一 source/target 任务写入数据库时钟租约。"""
    task_id = await session.scalar(
        select(data_sync_task.c.id).where(
            data_sync_task.c.source == desired.source,
            data_sync_task.c.source_schema == desired.source_schema,
            data_sync_task.c.source_table == desired.source_table,
            data_sync_task.c.target_table == desired.target_table,
        )
    )
    assert task_id is not None
    lease_token = uuid4().hex
    await session.execute(
        update(data_sync_task)
        .where(data_sync_task.c.id == task_id)
        .values(
            lease_token=lease_token,
            lease_expires_at=func.timestampadd(text("SECOND"), 120, func.now()),
            updated_at=func.now(),
        )
    )
    return ClaimedSyncTask(
        id=int(task_id),
        desired=desired,
        desired_hash=desired.desired_hash(),
        phase=SyncPhase.PENDING_SCHEMA,
        lease_token=lease_token,
        attempts=0,
        snapshot=None,
        captured=None,
        applied=None,
        last_backfill_key=None,
    )


@pytest.mark.integration
async def test_composite_primary_key_backfill_uses_lexicographic_cursor() -> None:
    """复合主键回填从最后完成的键之后继续，且不漏跨前缀行。"""
    table_name = f"sync_composite_{uuid4().hex[:12]}"
    source_writer = create_async_engine(
        make_url(app_config.mysql.url).set(database="source_demo")
    )
    desired = DesiredSyncTable(
        source="source_demo",
        source_schema="source_demo",
        source_table=table_name,
        target_table=table_name,
        columns=[
            DesiredColumn(
                id="tenant_id",
                name="tenant_id",
                data_type="BIGINT",
                nullable=False,
            ),
            DesiredColumn(
                id="order_id",
                name="order_id",
                data_type="BIGINT",
                nullable=False,
            ),
            DesiredColumn(
                id="amount",
                name="amount",
                data_type="INT",
                nullable=False,
            ),
        ],
        primary_key=["tenant_id", "order_id"],
        schema_fingerprint="b" * 64,
    )
    try:
        async with source_writer.begin() as connection:
            await connection.execute(
                text(
                    f"CREATE TABLE source_demo.{table_name} "
                    "(tenant_id BIGINT NOT NULL, order_id BIGINT NOT NULL, "
                    "amount INT NOT NULL, PRIMARY KEY (tenant_id, order_id)) "
                    "ENGINE=InnoDB"
                )
            )
            await connection.execute(
                text(
                    f"INSERT INTO source_demo.{table_name} "
                    "(tenant_id, order_id, amount) VALUES "
                    "(1, 1, 10), (1, 2, 20), (2, 1, 30)"
                )
            )
        rows = await read_backfill_batch(
            source_writer,
            desired,
            after_key=(1, 1),
            limit=2,
        )
        check_equal(
            "复合主键游标后的有序行",
            [(row["tenant_id"], row["order_id"]) for row in rows],
            [(1, 2), (2, 1)],
        )
    finally:
        async with source_writer.begin() as connection:
            await connection.execute(
                text(f"DROP TABLE IF EXISTS source_demo.{table_name}")
            )
        await source_writer.dispose()
