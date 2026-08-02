"""锁定 data_sync Core 元数据与 bootstrap DDL 的表列契约。"""

from pathlib import Path

from sqlalchemy import UniqueConstraint
from sqlglot import exp, parse
from tests.helpers.checks import check_equal

from data_sync.tables import (
    data_sync_event,
    data_sync_key_owner,
    data_sync_task,
)
from ddl_metadata.meta_projection.tables import (
    metadata_index_outbox,
    metadata_value_frequency,
    metadata_value_publication,
)
from settings import app_config


def test_data_sync_core_columns_match_bootstrap_script() -> None:
    """新环境 bootstrap 与运行时 Core 表必须逐列一致。"""
    script = (
        Path(__file__).parents[4] / "docs" / "docker" / "mysql" / "data_sync.sql"
    ).read_text(encoding="utf-8")
    creates = {
        statement.this.this.name: {
            item.this.name
            for item in statement.this.expressions
            if isinstance(item, exp.ColumnDef)
        }
        for statement in parse(script[script.index("USE data_sync;") :], read="mysql")
        if isinstance(statement, exp.Create)
        and isinstance(statement.this, exp.Schema)
    }
    tables = (
        data_sync_task,
        data_sync_event,
        data_sync_key_owner,
        metadata_index_outbox,
        metadata_value_frequency,
        metadata_value_publication,
    )
    check_equal(
        "data_sync 表集合",
        {table.name for table in tables},
        set(creates),
    )
    for table in tables:
        check_equal(
            f"{table.name} 列集合",
            {column.name for column in table.columns},
            creates[table.name],
        )
        check_equal(
            f"{table.name} 数据库",
            table.schema,
            app_config.data_sync.database,
        )


def test_sync_task_serializes_named_source_target_mapping() -> None:
    """数据库唯一约束封闭同一命名来源并发映射同一 DW 表的竞态。"""
    constraints = {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in data_sync_task.constraints
        if isinstance(constraint, UniqueConstraint) and constraint.name is not None
    }

    check_equal(
        "命名来源到目标表映射唯一",
        constraints["uq_data_sync_task_source_target"],
        ("source", "target_table"),
    )
