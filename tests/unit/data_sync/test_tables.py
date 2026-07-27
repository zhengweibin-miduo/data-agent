"""锁定 data_sync Core 元数据与 bootstrap DDL 的表列契约。"""

from pathlib import Path

from sqlglot import exp, parse
from tests.helpers.checks import check_equal

from data_agent.data_sync.tables import (
    data_sync_event,
    data_sync_key_owner,
    data_sync_task,
)
from data_agent.settings import app_config


def test_data_sync_core_columns_match_bootstrap_script() -> None:
    """新环境 bootstrap 与运行时 Core 表必须逐列一致。"""
    script = (
        Path(__file__).parents[3] / "docs" / "docker" / "mysql" / "data_sync.sql"
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
    tables = (data_sync_task, data_sync_event, data_sync_key_owner)
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
