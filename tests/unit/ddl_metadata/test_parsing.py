"""MySQL DDL 解析器检查。"""

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.parsing import parse_ddl
from data_agent.settings import app_config

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
        assert error.code == code
    else:
        raise AssertionError(f"DDL 应被 {code} 拒绝")


def test_ddl_parser() -> None:
    """覆盖多表、约束、注释、稳定 ID 和拒绝边界。"""
    schema = parse_ddl("test_source", DDL)
    repeated = parse_ddl("test_source", DDL)
    assert schema == repeated
    assert len(schema.tables) == 2
    assert schema.tables[0].comment == "customers"
    assert schema.tables[0].columns[0].comment == "customer key"
    assert schema.tables[0].columns[0].structural_role == "primary_key"
    assert schema.tables[1].qualified_name == "sales.fact_order"
    roles = {column.name: column.structural_role for column in schema.tables[1].columns}
    assert roles == {
        "order_id": "primary_key",
        "customer_id": "foreign_key",
        "amount": None,
    }
    assert len(schema.ddl_hash) == 64
    assert len(schema.schema_fingerprint) == 64

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
        assert error.code == "ddl_too_large"
    else:
        raise AssertionError("DDL 字节限制必须生效")
