"""DW 结构同步规划的确定性单元检查。"""

import pytest
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)

from data_agent.data_sync.models import DesiredColumn, DesiredSyncTable
from data_agent.data_sync.schema_sync import (
    CurrentColumn,
    CurrentTable,
    is_safe_widening,
    plan_schema_changes,
)
from data_agent.errors import DataAgentError


def _desired(*, amount_type: str = "DECIMAL(12, 2)") -> DesiredSyncTable:
    return DesiredSyncTable(
        source="local",
        source_schema="business",
        source_table="fact_order",
        target_table="fact_order",
        columns=[
            DesiredColumn(
                id="order_id",
                name="order_id",
                data_type="BIGINT",
                nullable=False,
            ),
            DesiredColumn(
                id="amount",
                name="amount",
                data_type=amount_type,
                nullable=False,
            ),
        ],
        primary_key=["order_id"],
        schema_fingerprint="a" * 64,
        metric_dependency_column_ids=["amount"],
    )


def test_plan_creates_missing_table_and_quotes_identifiers() -> None:
    """缺失目标表时生成一条带主键的安全 CREATE TABLE。"""
    statements = plan_schema_changes(
        database="dw",
        desired=_desired(),
        current=None,
    )
    check_equal("缺失目标表只生成一条 DDL", len(statements), 1)
    check_condition(
        "CREATE TABLE 引用数据库和表名",
        statements[0].startswith("CREATE TABLE dw.fact_order"),
        actual=statements[0],
    )
    check_condition(
        "CREATE TABLE 包含已接受主键",
        "PRIMARY KEY (order_id)" in statements[0],
        actual=statements[0],
    )
    check_condition(
        "字符串主键使用字节身份一致的 NO PAD 排序规则",
        "COLLATE utf8mb4_0900_bin" in statements[0],
        actual=statements[0],
    )


def test_boolean_alias_matches_mysql_introspection() -> None:
    """MySQL 将 BOOLEAN introspect 为 tinyint(1) 后仍保持幂等。"""
    desired = _desired(amount_type="BOOLEAN")
    current = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "tinyint(1)", False),
        ),
        primary_key=("order_id",),
    )
    check_equal(
        "BOOLEAN 与 tinyint(1) 等价",
        plan_schema_changes(database="dw", desired=desired, current=current),
        [],
    )


def test_plan_adds_and_safely_widens_columns() -> None:
    """已有表只添加缺失列或扩大被支持的字段类型。"""
    missing_column = CurrentTable(
        columns=(CurrentColumn("order_id", "bigint", False),),
        primary_key=("order_id",),
    )
    check_equal(
        "缺失列生成 ADD COLUMN",
        plan_schema_changes(
            database="dw",
            desired=_desired(),
            current=missing_column,
        ),
        ["ALTER TABLE dw.fact_order ADD COLUMN amount DECIMAL(12, 2) NOT NULL"],
    )
    narrow_decimal = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "decimal(10,2)", False),
        ),
        primary_key=("order_id",),
    )
    check_equal(
        "安全扩大 DECIMAL 生成 MODIFY COLUMN",
        plan_schema_changes(
            database="dw",
            desired=_desired(),
            current=narrow_decimal,
        ),
        ["ALTER TABLE dw.fact_order MODIFY COLUMN amount DECIMAL(12, 2) NOT NULL"],
    )


@pytest.mark.parametrize(
    ("current", "desired", "expected"),
    [
        ("INT", "BIGINT", True),
        ("INT UNSIGNED", "BIGINT", True),
        ("BIGINT UNSIGNED", "BIGINT", False),
        ("VARCHAR(32)", "VARCHAR(64)", True),
        ("VARBINARY(64)", "VARBINARY(32)", False),
        ("DECIMAL(10,2)", "DECIMAL(12,3)", True),
        ("DECIMAL(10,2)", "DECIMAL(10,3)", False),
    ],
)
def test_safe_widening_matrix(current: str, desired: str, expected: bool) -> None:
    """安全扩宽矩阵覆盖整数、变长字段和定点数边界。"""
    check_equal(
        f"{current} -> {desired}",
        is_safe_widening(current, desired),
        expected,
    )


def test_plan_rejects_destructive_difference() -> None:
    """额外字段、主键变化和缩窄类型均成为非重试结构冲突。"""
    destructive = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "decimal(18,4)", False),
            CurrentColumn("legacy", "varchar(32)", True),
        ),
        primary_key=("order_id",),
    )
    try:
        plan_schema_changes(
            database="dw",
            desired=_desired(),
            current=destructive,
        )
    except Exception as error:
        check_exception("破坏性结构差异被拒绝", error, DataAgentError)
        check_equal(
            "结构冲突不可重试",
            getattr(error, "retryable", None),
            False,
        )
    else:
        fail_check(
            "破坏性结构差异被拒绝",
            actual="未抛出异常",
            expected="DataAgentError",
        )
