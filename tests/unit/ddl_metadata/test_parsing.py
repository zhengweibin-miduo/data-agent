"""MySQL DDL 解析器检查。"""

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.parsing import parse_ddl
from data_agent.settings import app_config
from tests.helpers.checks import check_equal, check_exception, fail_check

DDL = """
CREATE TABLE dim_customer (
    customer_id BIGINT COMMENT 'customer key',
    name VARCHAR(100),
    PRIMARY KEY (customer_id)
) COMMENT='customers';
CREATE TABLE sales.fact_order (
    order_id BIGINT PRIMARY KEY,
    customer_id BIGINT,
    amount DECIMAL(10, 2),
    CONSTRAINT fk_customer FOREIGN KEY (customer_id)
        REFERENCES dim_customer(customer_id)
) COMMENT='orders';
"""


def _assert_rejected(ddl: str, code: str) -> None:
    """断言 DDL 被指定错误拒绝。"""
    try:
        parse_ddl("test_source", ddl)
    except DDLMetadataError as error:
        check_exception("_assert_rejected 捕获预期异常", error, DDLMetadataError)
        check_equal("_assert_rejected 检查点 1", error.code, code)
    else:
        fail_check(
            "_assert_rejected",
            actual="未抛出预期异常",
            expected=f"DDL 应被 {code} 拒绝",
        )


def test_ddl_parser() -> None:
    """覆盖多表、约束、注释、稳定 ID 和拒绝边界。"""
    schema = parse_ddl("test_source", DDL)
    repeated = parse_ddl("test_source", DDL)
    check_equal("test_ddl_parser 检查点 1", schema, repeated)
    check_equal("test_ddl_parser 检查点 2", len(schema.tables), 2)
    check_equal(
        "test_ddl_parser 检查点 3",
        schema.tables[0].comment,
        "customers",
    )
    check_equal(
        "test_ddl_parser 检查点 4",
        schema.tables[0].columns[0].comment,
        "customer key",
    )
    check_equal(
        "test_ddl_parser 检查点 5",
        schema.tables[0].columns[0].structural_role,
        "primary_key",
    )
    check_equal(
        "test_ddl_parser 检查点 6",
        schema.tables[1].qualified_name,
        "sales.fact_order",
    )
    roles = {column.name: column.structural_role for column in schema.tables[1].columns}
    check_equal(
        "test_ddl_parser 检查点 7",
        roles,
        {
            "order_id": "primary_key",
            "customer_id": "foreign_key",
            "amount": None,
        },
    )
    check_equal("test_ddl_parser 检查点 8", len(schema.ddl_hash), 64)
    check_equal(
        "test_ddl_parser 检查点 9",
        len(schema.schema_fingerprint),
        64,
    )

    _assert_rejected("ALTER TABLE x ADD y INT", "unsupported_statement")
    _assert_rejected("CREATE VIEW x AS SELECT 1", "unsupported_statement")
    _assert_rejected("CREATE TABLE x (", "malformed_ddl")
    _assert_rejected(
        "CREATE TABLE x (a INT, A VARCHAR(2))",
        "duplicate_column",
    )
    _assert_rejected(
        "CREATE TABLE x (a INT); CREATE TABLE X (a INT)",
        "duplicate_table",
    )

    tiny_limits = app_config.api.model_copy(
        update={"max_ddl_bytes": 8, "max_tables": 1, "max_columns": 1}
    )
    try:
        parse_ddl("test_source", DDL, tiny_limits)
    except DDLMetadataError as error:
        check_exception("test_ddl_parser 捕获预期异常", error, DDLMetadataError)
        check_equal(
            "test_ddl_parser 检查点 10",
            error.code,
            "ddl_too_large",
        )
    else:
        fail_check(
            "test_ddl_parser", actual="未抛出预期异常", expected="DDL 字节限制必须生效"
        )
