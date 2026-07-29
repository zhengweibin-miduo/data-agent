"""DW 结构同步规划的确定性单元检查。"""

from unittest.mock import AsyncMock

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
    DWSchemaSynchronizer,
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


async def test_existing_table_requires_ownership_for_every_row() -> None:
    """非空目标表中只要存在未登记行就拒绝开始同步。"""
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[2, 1])
    synchronizer = DWSchemaSynchronizer(session, database="dw")

    with pytest.raises(DataAgentError, match="未登记或多余"):
        await synchronizer._validate_existing_provenance(_desired())


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


@pytest.mark.parametrize(
    ("desired_type", "current_type"),
    [("DECIMAL", "decimal(10,0)"), ("DECIMAL(10)", "decimal(10,0)")],
)
def test_decimal_defaults_match_mysql_introspection(
    desired_type: str, current_type: str
) -> None:
    """MySQL 补全 DECIMAL 默认精度和 scale 后仍保持幂等。"""
    desired = _desired(amount_type=desired_type)
    current = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", current_type, False),
        ),
        primary_key=("order_id",),
    )
    check_equal(
        "DECIMAL 默认参数等价",
        plan_schema_changes(database="dw", desired=desired, current=current),
        [],
    )


def test_plan_accepts_nullable_columns_from_other_sources() -> None:
    """共享目标中其他来源贡献的 nullable 字段不阻塞当前来源。"""
    current = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "decimal(12,2)", False),
            CurrentColumn("crm_note", "varchar(100)", True),
        ),
        primary_key=("order_id",),
    )
    check_equal(
        "其他来源 nullable 字段可安全省略",
        plan_schema_changes(
            database="dw",
            desired=_desired(),
            current=current,
            compatible_extra_columns={"crm_note"},
        ),
        [],
    )


def test_plan_rejects_required_columns_from_other_sources() -> None:
    """共享目标中其他来源的必填字段不能由当前来源安全省略。"""
    current = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "decimal(12,2)", False),
            CurrentColumn("required_value", "int", False),
        ),
        primary_key=("order_id",),
    )
    with pytest.raises(DataAgentError, match="待删除字段"):
        plan_schema_changes(
            database="dw",
            desired=_desired(),
            current=current,
            compatible_extra_columns=set(),
        )


@pytest.mark.parametrize(
    ("current", "reason"),
    [
        (
            CurrentTable(
                columns=(CurrentColumn("order_id", "bigint", False),),
                primary_key=("order_id",),
                engine="MyISAM",
            ),
            "InnoDB",
        ),
        (
            CurrentTable(
                columns=(CurrentColumn("order_id", "bigint", False),),
                primary_key=("order_id",),
                unique_indexes=("uq_email",),
            ),
            "额外唯一索引",
        ),
    ],
)
def test_plan_rejects_non_transactional_or_extra_unique_constraints(
    current: CurrentTable,
    reason: str,
) -> None:
    """已有目标表必须保持事务原子性和仅主键冲突语义。"""
    try:
        plan_schema_changes(database="dw", desired=_desired(), current=current)
    except DataAgentError as error:
        check_equal("不兼容目标表错误码", error.code, "dw_schema_conflict")
        check_condition("错误说明具体不兼容项", reason in str(error), actual=str(error))
    else:
        fail_check("不兼容目标表被拒绝", actual=current, expected=reason)


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

    already_wider = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "decimal(18,4)", False),
        ),
        primary_key=("order_id",),
    )
    check_equal(
        "共享目标已更宽时无需缩窄",
        plan_schema_changes(database="dw", desired=_desired(), current=already_wider),
        [],
    )


def test_plan_accepts_nullable_target_for_required_source_column() -> None:
    """目标允许 NULL 是来源 NOT NULL 契约的安全超集。"""
    current = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "decimal(12,2)", True),
        ),
        primary_key=("order_id",),
    )
    check_equal(
        "更宽松目标可空性无需修改",
        plan_schema_changes(database="dw", desired=_desired(), current=current),
        [],
    )


def test_plan_rejects_non_binary_string_primary_key_collation() -> None:
    """已有字符串主键必须与字节 ownership 使用相同等价语义。"""
    desired = _desired()
    desired.columns[0] = DesiredColumn(
        id="order_id", name="order_id", data_type="VARCHAR(64)", nullable=False
    )
    current = CurrentTable(
        columns=(
            CurrentColumn("order_id", "varchar(64)", False, "utf8mb4_general_ci"),
            CurrentColumn("amount", "decimal(12,2)", False),
        ),
        primary_key=("order_id",),
    )
    with pytest.raises(DataAgentError, match="utf8mb4_0900_bin"):
        plan_schema_changes(database="dw", desired=desired, current=current)


def test_enum_literals_keep_original_case() -> None:
    """生成 DDL 时不改写 ENUM 业务字面值。"""
    desired = _desired(amount_type="ENUM('pending','Done')")
    statements = plan_schema_changes(database="dw", desired=desired, current=None)
    check_condition(
        "ENUM 字面量保留大小写",
        "'pending'" in statements[0] and "'Done'" in statements[0],
        actual=statements[0],
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
