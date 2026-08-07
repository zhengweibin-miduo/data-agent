"""锁定 Meta Core 元数据与 bootstrap DDL 的列契约。"""

from pathlib import Path

from sqlglot import exp, parse

from ddl_metadata.persistence.metadata_repository import authority_scope_key
from ddl_metadata.persistence.tables import (
    column_info,
    column_metric,
    metric_info,
    physical_schema_authority,
    table_info,
)
from tests.helpers.checks import check_equal


def test_meta_core_columns_match_bootstrap_script() -> None:
    """Meta bootstrap 与 SQLAlchemy Core 定义必须逐列一致。"""
    script = (
        Path(__file__).parents[4] / "docs" / "docker" / "mysql" / "meta.sql"
    ).read_text(encoding="utf-8")
    creates = {
        statement.this.this.name: {
            item.this.name
            for item in statement.this.expressions
            if isinstance(item, exp.ColumnDef)
        }
        for statement in parse(script[script.index("USE meta;") :], read="mysql")
        if isinstance(statement, exp.Create)
        and isinstance(statement.this, exp.Schema)
    }
    tables = (
        table_info,
        column_info,
        metric_info,
        column_metric,
        physical_schema_authority,
    )
    check_equal("Meta 表集合", {table.name for table in tables}, set(creates))
    for table in tables:
        check_equal(
            f"{table.name} 列集合",
            {column.name for column in table.columns},
            creates[table.name],
        )
    check_equal("字段资格列不可为空", column_info.c.index_profile.nullable, False)
    check_equal("指标事实表列不可为空", metric_info.c.fact_table_id.nullable, False)


def test_authority_scope_key_is_order_independent_and_scope_specific() -> None:
    """局部 accepted 快照按表集合共存，同一集合更新时命中同一权威槽位。"""
    check_equal(
        "表顺序不影响 scope key",
        authority_scope_key(["table-a", "table-b"]),
        authority_scope_key(["table-b", "table-a"]),
    )
    assert authority_scope_key(["table-a"]) != authority_scope_key(["table-b"])
