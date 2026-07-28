"""真实 MySQL 上的 DW 回填与 Binlog 收敛检查。"""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.helpers.checks import check_condition, check_equal

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
)
from data_agent.data_sync.repository import DataSyncRepository
from data_agent.data_sync.schema_sync import DWSchemaSynchronizer
from data_agent.errors import DataAgentError
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.settings import app_config


@pytest.mark.integration
async def test_backfill_then_binlog_converges() -> None:
    """分块历史行与后续写改删事件最终收敛到同一 DW 表。"""
    table_name = f"sync_fact_{uuid4().hex[:12]}"
    source_settings = app_config.data_sync.sources["source_demo"]
    source = MySQLSourceClient(
        "source_demo",
        source_settings,
        connect_timeout_seconds=5,
        read_timeout_seconds=5,
    )
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
                id="id",
                name="id",
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
        primary_key=["id"],
        schema_fingerprint="a" * 64,
    )
    MySQLDatabase.initialize()
    task_id: int | None = None
    try:
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

        # 步骤二：持久化并领取任务，创建 DW 结构后按主键回填历史数据。
        async with MySQLDatabase.session() as session:
            repository = DataSyncRepository(session)
            await repository.upsert_desired([desired])
            tasks = await repository.claim_tasks(
                limit=1,
                lease_seconds=120,
                max_attempts=3,
            )
        check_equal("领取一个同步任务", len(tasks), 1)
        task = tasks[0]
        task_id = task.id
        async with MySQLDatabase.session() as session:
            await DWSchemaSynchronizer(
                session,
                database=app_config.data_sync.dw_database,
            ).synchronize(desired)
            repository = DataSyncRepository(session)
            await repository.record_snapshot(task, baseline)
            await repository.advance_captured_coordinate(task, baseline)
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
                        source="source_demo",
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
        # 步骤八：只清理本测试 UUID 命名的源表、目标表和控制状态。
        async with source_writer.begin() as connection:
            await connection.execute(
                text(f"DROP TABLE IF EXISTS source_demo.{table_name}")
            )
        async with MySQLDatabase.session() as session:
            if task_id is not None:
                await session.execute(
                    text(
                        "DELETE FROM data_sync.data_sync_event "
                        "WHERE task_id=:task_id"
                    ),
                    {"task_id": task_id},
                )
                await session.execute(
                    text("DELETE FROM data_sync.data_sync_task WHERE id=:task_id"),
                    {"task_id": task_id},
                )
            await session.execute(
                text(
                    "DELETE FROM data_sync.data_sync_key_owner "
                    "WHERE target_table=:target_table"
                ),
                {"target_table": table_name},
            )
            await session.execute(text(f"DROP TABLE IF EXISTS dw.{table_name}"))
        await source.close()
        await source_writer.dispose()
        await MySQLDatabase.close()


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
