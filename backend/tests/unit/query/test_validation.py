"""只读 SQLGlot 门禁的行为测试。"""

import asyncio
import threading

import pytest

import query.domain as query_domain
from models.physical import (
    PhysicalColumn,
    PhysicalRelationship,
    PhysicalSchema,
    PhysicalTable,
)
from query.domain import (
    QueryContext,
    QueryDraft,
    QueryIntent,
    QueryParameter,
    QueryType,
    validate_query,
)


def _context() -> QueryContext:
    """构造一张当前 DDL 权威表的查询上下文。"""
    return QueryContext(
        physical_schema=PhysicalSchema(
            source="erp",
            canonical_ddl="CREATE TABLE orders (id BIGINT, amount DECIMAL(10,2))",
            ddl_hash="ddl",
            schema_fingerprint="schema",
            tables=[
                PhysicalTable(
                    id="table-orders",
                    name="orders",
                    qualified_name="orders",
                    columns=[
                        PhysicalColumn(id="column-id", name="id", data_type="BIGINT"),
                        PhysicalColumn(
                            id="column-amount",
                            name="amount",
                            data_type="DECIMAL(10,2)",
                        ),
                    ],
                ),
                PhysicalTable(
                    id="table-customers",
                    name="customers",
                    qualified_name="customers",
                    columns=[
                        PhysicalColumn(
                            id="column-customer-id", name="id", data_type="BIGINT"
                        )
                    ],
                ),
            ],
            relationships=[
                PhysicalRelationship(
                    source_table_id="table-orders",
                    source_column_id="column-id",
                    target_table="customers",
                    target_column="id",
                )
            ],
        ),
    )


def _draft(
    sql: str,
    *,
    params: dict[str, QueryParameter] | None = None,
) -> QueryDraft:
    """构造声明与 AST 对齐的订单金额草稿。"""
    return QueryDraft(
        sql=sql,
        params=params or {},
        table_ids=["table-orders"],
        column_ids=["column-amount"],
    )


async def test_valid_aggregate_keeps_complete_result_without_forced_limit() -> None:
    """没有业务 Top-N 的聚合查询通过且不被注入总量 LIMIT。"""
    result = await validate_query(
        _draft("SELECT SUM(o.amount) AS total FROM dw.orders AS o"),
        _context(),
        QueryIntent(query_type=QueryType.AGGREGATE, measure_quotes=["金额"]),
        dw_database="dw",
    )

    assert result.validated is not None
    assert result.validated.sql == "SELECT SUM(o.amount) AS total FROM dw.orders AS o"
    assert result.validated.target_tables == ("orders",)


async def test_count_star_is_not_projection_star() -> None:
    """COUNT(*) 保留标准聚合语义而普通 SELECT 星号仍被拒绝。"""
    draft = QueryDraft(
        sql="SELECT COUNT(*) AS total FROM dw.orders",
        table_ids=["table-orders"],
    )

    result = await validate_query(
        draft,
        _context(),
        QueryIntent(query_type=QueryType.AGGREGATE, measure_quotes=["数量"]),
        dw_database="dw",
    )

    assert result.validated is not None


@pytest.mark.parametrize(
    ("sql", "params", "code"),
    [
        (
            "SELECT o.amount FROM dw.orders o; DELETE FROM dw.orders",
            {},
            "statement_count",
        ),
        ("DELETE FROM dw.orders", {}, "select_only"),
        ("SELECT * FROM dw.orders", {}, "select_star"),
        ("SELECT o.amount FROM mysql.orders o", {}, "schema_forbidden"),
        ("SELECT SLEEP(1) FROM dw.orders", {}, "function_forbidden"),
        ("SELECT o.amount FROM dw.orders o FOR UPDATE", {}, "lock_forbidden"),
        (
            "SELECT o.amount FROM dw.orders o CROSS JOIN dw.customers c",
            {},
            "join_forbidden",
        ),
        (
            "SELECT o.amount FROM dw.orders o "
            "JOIN dw.customers c ON o.amount = c.id",
            {},
            "join_unsupported",
        ),
        (
            "SELECT o.amount FROM dw.orders o WHERE o.amount = 10",
            {},
            "literal_forbidden",
        ),
        (
            "SELECT o.amount FROM dw.orders o WHERE o.amount = TRUE",
            {},
            "literal_forbidden",
        ),
        (
            "SELECT o.amount FROM dw.orders o WHERE o.amount = :amount",
            {},
            "parameter_mismatch",
        ),
        ("SELECT o.amount FROM dw.orders o LIMIT 10", {}, "limit_unexpected"),
    ],
)
async def test_unsafe_sql_fails_closed(
    sql: str, params: dict[str, QueryParameter], code: str
) -> None:
    """静态门禁为危险 SQL 返回稳定问题代码。"""
    result = await validate_query(
        _draft(sql, params=params),
        _context(),
        QueryIntent(query_type=QueryType.DETAIL, measure_quotes=["金额"]),
        dw_database="dw",
    )

    assert result.issues[0].code == code


async def test_cte_validates_physical_column_but_allows_outer_projection() -> None:
    """CTE 内字段仍按物理 allowlist 校验，外层可引用其投影名称。"""
    result = await validate_query(
        _draft(
            "WITH scoped AS (SELECT o.amount FROM dw.orders AS o) "
            "SELECT amount FROM scoped"
        ),
        _context(),
        QueryIntent(query_type=QueryType.DETAIL, measure_quotes=["金额"]),
        dw_database="dw",
    )

    assert result.validated is not None

    invalid = await validate_query(
        _draft(
            "WITH scoped AS (SELECT o.missing FROM dw.orders AS o) "
            "SELECT missing FROM scoped"
        ),
        _context(),
        QueryIntent(query_type=QueryType.DETAIL, measure_quotes=["金额"]),
        dw_database="dw",
    )
    assert invalid.issues[0].code == "column_unknown"


async def test_named_placeholder_and_fk_join_are_accepted() -> None:
    """绑定值与当前 DDL 外键支持的显式 JOIN 可安全通过。"""
    draft = QueryDraft(
        sql=(
            "SELECT o.amount FROM dw.orders AS o "
            "JOIN dw.customers AS c ON o.id = c.id WHERE o.amount > :minimum"
        ),
        params={"minimum": 10},
        table_ids=["table-orders", "table-customers"],
        column_ids=["column-amount", "column-id", "column-customer-id"],
    )

    result = await validate_query(
        draft,
        _context(),
        QueryIntent(query_type=QueryType.DETAIL, measure_quotes=["金额"]),
        dw_database="dw",
    )

    assert result.validated is not None


async def test_explicit_top_n_is_preserved_exactly() -> None:
    """用户明确的 Top-N 数量原样通过，不被执行层改写。"""
    result = await validate_query(
        _draft("SELECT o.amount FROM dw.orders AS o LIMIT 10"),
        _context(),
        QueryIntent(
            query_type=QueryType.RANKING,
            measure_quotes=["金额"],
            limit=10,
        ),
        dw_database="dw",
    )

    assert result.validated is not None
    assert result.validated.sql.endswith("LIMIT 10")


async def test_order_by_projection_alias_keeps_physical_validation() -> None:
    """排序可引用已验证投影别名，不把别名误判成未知物理字段。"""
    result = await validate_query(
        _draft(
            "SELECT SUM(o.amount) AS total FROM dw.orders AS o "
            "ORDER BY total DESC LIMIT 10"
        ),
        _context(),
        QueryIntent(
            query_type=QueryType.RANKING,
            measure_quotes=["金额"],
            limit=10,
        ),
        dw_database="dw",
    )

    assert result.validated is not None


async def test_sqlglot_validation_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLGlot 同步解析运行在线程中，解析期间事件循环仍可调度。"""
    parse_started = threading.Event()
    release_parse = threading.Event()
    original_parse = query_domain.sqlglot.parse

    def blocking_parse(sql: str, *, read: str):
        """用线程事件暂停真实 SQLGlot 解析。"""
        parse_started.set()
        assert release_parse.wait(timeout=1)
        return original_parse(sql, read=read)

    monkeypatch.setattr(query_domain.sqlglot, "parse", blocking_parse)
    validation = asyncio.create_task(
        validate_query(
            _draft("SELECT o.amount FROM dw.orders AS o"),
            _context(),
            QueryIntent(query_type=QueryType.DETAIL, measure_quotes=["金额"]),
            dw_database="dw",
        )
    )
    assert await asyncio.to_thread(parse_started.wait, 1)
    progressed = asyncio.Event()
    asyncio.get_running_loop().call_soon(progressed.set)
    await asyncio.wait_for(progressed.wait(), timeout=0.1)
    release_parse.set()

    result = await validation

    assert result.validated is not None
