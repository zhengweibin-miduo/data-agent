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
    FilterIntent,
    QueryContext,
    QueryDraft,
    QueryIntent,
    QueryParameter,
    QueryType,
    SortIntent,
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
                        PhysicalColumn(
                            id="column-created-at",
                            name="created_at",
                            data_type="DATE",
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
        _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["金额"],
        ),
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
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="count",
            aggregation_quote="数量",
            measure_quotes=["数量"],
        ),
        dw_database="dw",
    )

    assert result.validated is not None


def test_top_n_requires_exact_user_evidence() -> None:
    """Top-N 数量必须由包含同值的用户原文证明。"""
    intent = QueryIntent(query_type=QueryType.RANKING, limit=10)

    with pytest.raises(ValueError, match="Top-N"):
        intent.validate_evidence(["查询销售额"])


def test_time_range_requires_normalized_predicate_contract() -> None:
    """独立时间证据不得在缺少可验证谓词契约时进入规划。"""
    intent = QueryIntent(
        query_type=QueryType.TREND,
        time_quote="2026-08-01",
        time_column_quote="日期",
        grain="day",
    )

    with pytest.raises(ValueError, match="时间范围"):
        intent.validate_evidence(["按日期查看 2026-08-01 的趋势"])


@pytest.mark.parametrize(
    ("limit", "quote"),
    [(10, "前100名"), (1, "前10名"), (10, "2025年前100名")],
)
def test_top_n_rejects_partial_or_ambiguous_numeric_evidence(
    limit: int, quote: str
) -> None:
    """Top-N 只接受证据中唯一且完整匹配的十进制数量。"""
    intent = QueryIntent(
        query_type=QueryType.RANKING,
        limit=limit,
        limit_quote=quote,
    )

    with pytest.raises(ValueError, match="Top-N"):
        intent.validate_evidence([quote])


async def test_nested_cte_name_does_not_hide_outer_physical_table() -> None:
    """嵌套 CTE 名称不能把外层同名真实表伪装成可见 CTE。"""
    draft = QueryDraft(
        sql=(
            "SELECT COUNT(*) FROM secret, "
            "(WITH secret AS (SELECT o.id FROM dw.orders o) SELECT id FROM secret) x"
        ),
        table_ids=["table-orders"],
        column_ids=["column-id"],
    )

    result = await validate_query(
        draft,
        _context(),
        QueryIntent(query_type=QueryType.AGGREGATE, measure_quotes=["数量"]),
        dw_database="dw",
    )

    assert result.issues[0].code == "schema_forbidden"


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
            "SELECT o.amount FROM dw.orders o JOIN dw.customers c ON o.amount = c.id",
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
        _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["金额"],
            filters=[
                FilterIntent(
                    column_quote="金额",
                    operator="gt",
                    value_quotes=["10"],
                )
            ],
        ),
        dw_database="dw",
    )

    assert result.validated is not None


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        ("SELECT o.amount FROM dw.orders o WHERE o.amount > :minimum", {"minimum": 10}),
        ("SELECT o.amount FROM dw.orders o WHERE o.amount < :minimum", {"minimum": 10}),
        (
            "SELECT o.amount FROM dw.orders o WHERE o.amount > :minimum",
            {"minimum": 999},
        ),
    ],
)
async def test_predicates_must_exactly_match_filter_intent(
    sql: str, params: dict[str, QueryParameter]
) -> None:
    """额外谓词、反向运算符和错误参数值均在自动执行前拒绝。"""
    context = _context().model_copy(update={"bindings": {"金额": "column-amount"}})
    filters = (
        []
        if params.get("minimum") == 10 and ">" in sql
        else [FilterIntent(column_quote="金额", operator="gt", value_quotes=["10"])]
    )

    result = await validate_query(
        _draft(sql, params=params),
        context,
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["金额"],
            filters=filters,
        ),
        dw_database="dw",
    )

    assert result.issues[0].code == "predicate_mismatch"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT o.amount FROM dw.orders o WHERE NOT (o.amount > :minimum)",
        "SELECT o.amount FROM dw.orders o WHERE o.amount NOT IN (:minimum)",
        "SELECT o.amount FROM dw.orders o WHERE o.amount NOT LIKE :minimum",
    ],
)
async def test_negated_predicates_fail_closed(sql: str) -> None:
    """过滤谓词被 NOT 包裹后不得冒充未取反的可信意图。"""
    result = await validate_query(
        _draft(sql, params={"minimum": 10}),
        _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["金额"],
            filters=[
                FilterIntent(column_quote="金额", operator="gt", value_quotes=["10"])
            ],
        ),
        dw_database="dw",
    )
    assert result.issues[0].code == "predicate_mismatch"


async def test_filter_predicates_reject_or_boolean_structure() -> None:
    """未建模布尔关系时多个可信过滤只能由 AND 连接。"""
    result = await validate_query(
        _draft(
            "SELECT o.amount FROM dw.orders o WHERE o.amount > :minimum OR o.id = :id",
            params={"minimum": 10, "id": 1},
        ).model_copy(update={"column_ids": ["column-amount", "column-id"]}),
        _context().model_copy(
            update={"bindings": {"金额": "column-amount", "订单": "column-id"}}
        ),
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["金额"],
            filters=[
                FilterIntent(column_quote="金额", operator="gt", value_quotes=["10"]),
                FilterIntent(column_quote="订单", operator="eq", value_quotes=["1"]),
            ],
        ),
        dw_database="dw",
    )
    assert result.issues[0].code == "predicate_mismatch"


async def test_aggregate_must_be_in_top_level_projection() -> None:
    """标量子查询中的聚合不能把顶层明细伪装成聚合结果。"""
    draft = QueryDraft(
        sql="SELECT o.amount, (SELECT SUM(i.amount) FROM dw.orders i) FROM dw.orders o",
        table_ids=["table-orders"],
        column_ids=["column-amount"],
    )
    result = await validate_query(
        draft,
        _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["金额"],
        ),
        dw_database="dw",
    )
    assert result.issues[0].code == "query_shape_mismatch"


async def test_aggregate_operand_must_be_selected_measure() -> None:
    """正确聚合函数不得通过无效引用掩盖错误操作数。"""
    result = await validate_query(
        _draft("SELECT SUM(o.id + 0 * o.amount) FROM dw.orders o").model_copy(
            update={"column_ids": ["column-id", "column-amount"]}
        ),
        _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["金额"],
        ),
        dw_database="dw",
    )
    assert result.issues[0].code == "aggregation_operand_mismatch"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT DISTINCT o.amount FROM dw.orders o",
        "SELECT COUNT(DISTINCT o.amount) FROM dw.orders o",
    ],
)
async def test_distinct_requires_explicit_intent_contract(sql: str) -> None:
    """当前无唯一化意图证据时明细和聚合 DISTINCT 都失败关闭。"""
    result = await validate_query(
        _draft(sql),
        _context(),
        QueryIntent(query_type=QueryType.DETAIL, measure_quotes=["金额"]),
        dw_database="dw",
    )
    assert result.issues[0].code == "distinct_forbidden"


@pytest.mark.parametrize("value", ["foo", "foo%", "%foo", "%foo%%"])
async def test_contains_requires_exact_both_sided_pattern(value: str) -> None:
    """Contains 只接受受控的双侧通配模式。"""
    result = await validate_query(
        _draft(
            "SELECT o.amount FROM dw.orders o WHERE o.amount LIKE :value",
            params={"value": value},
        ),
        _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["金额"],
            filters=[
                FilterIntent(
                    column_quote="金额", operator="contains", value_quotes=["foo"]
                )
            ],
        ),
        dw_database="dw",
    )
    assert (result.validated is not None) is (value == "%foo%")


def test_evidence_rejects_whitespace_only_quotes() -> None:
    """空白槽位不能冒充用户原文证据。"""
    with pytest.raises(ValueError, match="关键短语"):
        QueryIntent(
            query_type=QueryType.DETAIL, dimension_quotes=[" "]
        ).validate_evidence(["查询金额 "])


async def test_aggregate_function_must_match_evidenced_operation() -> None:
    """用户要求平均值时不能由 SUM 等其他聚合函数替代。"""
    intent = QueryIntent(
        query_type=QueryType.AGGREGATE,
        aggregation="avg",
        aggregation_quote="平均",
        measure_quotes=["金额"],
    )
    intent.validate_evidence(["平均金额"])
    result = await validate_query(
        _draft("SELECT SUM(o.amount) FROM dw.orders o"),
        _context(),
        intent,
        dw_database="dw",
    )
    assert result.issues[0].code == "aggregation_mismatch"


async def test_explicit_top_n_is_preserved_exactly() -> None:
    """用户明确的 Top-N 数量原样通过，不被执行层改写。"""
    result = await validate_query(
        _draft("SELECT o.amount FROM dw.orders AS o ORDER BY o.amount DESC LIMIT 10"),
        _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
        QueryIntent(
            query_type=QueryType.RANKING,
            measure_quotes=["金额"],
            sorts=[SortIntent(quote="金额", direction="desc")],
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
        _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
        QueryIntent(
            query_type=QueryType.RANKING,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["金额"],
            sorts=[SortIntent(quote="金额", direction="desc")],
            limit=10,
        ),
        dw_database="dw",
    )

    assert result.validated is not None


async def test_trend_requires_bound_time_predicate_and_group() -> None:
    """趋势查询的时间范围与粒度必须同时落入 SQL AST。"""
    context = _context().model_copy(
        update={"bindings": {"日期": "column-created-at", "金额": "column-amount"}}
    )
    intent = QueryIntent(
        query_type=QueryType.TREND,
        aggregation="sum",
        aggregation_quote="总和",
        measure_quotes=["金额"],
        time_quote="2026-08-01",
        time_column_quote="日期",
        time_filter=FilterIntent(
            column_quote="日期", operator="eq", value_quotes=["2026-08-01"]
        ),
        grain="day",
    )
    draft = QueryDraft(
        sql=(
            "SELECT DATE(o.created_at) AS day, SUM(o.amount) AS total "
            "FROM dw.orders o WHERE o.created_at = :day GROUP BY DATE(o.created_at)"
        ),
        params={"day": "2026-08-01"},
        table_ids=["table-orders"],
        column_ids=["column-created-at", "column-amount"],
    )

    result = await validate_query(draft, context, intent, dw_database="dw")

    assert result.validated is not None


async def test_quarter_grain_requires_year_coordinate() -> None:
    """季度趋势必须保留年份坐标，避免跨年同季度合并。"""
    context = _context().model_copy(
        update={"bindings": {"日期": "column-created-at", "金额": "column-amount"}}
    )
    intent = QueryIntent(
        query_type=QueryType.TREND,
        aggregation="sum",
        aggregation_quote="总和",
        measure_quotes=["金额"],
        time_quote="2024到2025",
        time_column_quote="日期",
        time_filter=FilterIntent(
            column_quote="日期", operator="gte", value_quotes=["2024-01-01"]
        ),
        grain="quarter",
    )
    draft = QueryDraft(
        sql=(
            "SELECT QUARTER(o.created_at), SUM(o.amount) FROM dw.orders o "
            "WHERE o.created_at >= :start GROUP BY QUARTER(o.created_at)"
        ),
        params={"start": "2024-01-01"},
        table_ids=["table-orders"],
        column_ids=["column-created-at", "column-amount"],
    )
    result = await validate_query(draft, context, intent, dw_database="dw")
    assert result.issues[0].code == "time_grain_mismatch"


@pytest.mark.parametrize(
    ("sql", "code"),
    [
        ("SELECT o.amount FROM dw.orders o", "query_shape_mismatch"),
        ("SELECT SUM(o.amount) FROM dw.orders o", "query_shape_mismatch"),
        ("SELECT o.amount FROM dw.orders o LIMIT 10 OFFSET 1", "offset_forbidden"),
        ("SELECT o.amount FROM dw.orders o LIMIT 1, 10", "offset_forbidden"),
    ],
)
async def test_query_shape_and_offset_are_enforced(sql: str, code: str) -> None:
    """查询形态不匹配或携带无证据偏移时失败关闭。"""
    query_type = QueryType.AGGREGATE if "SUM" not in sql else QueryType.DETAIL
    intent = QueryIntent(
        query_type=query_type,
        measure_quotes=["金额"],
        limit=10 if "LIMIT" in sql else None,
    )
    result = await validate_query(_draft(sql), _context(), intent, dw_database="dw")
    assert result.issues[0].code == code


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT o.amount FROM dw.orders o JOIN dw.customers c "
        "ON o.id = c.id AND o.amount = c.id",
        "SELECT o.amount FROM dw.orders o JOIN dw.customers c "
        "ON o.id = c.id OR o.amount = c.id",
    ],
)
async def test_join_rejects_every_unauthorized_condition(sql: str) -> None:
    """合法 FK 旁的额外非授权连接条件不能被 any 校验掩盖。"""
    draft = QueryDraft(
        sql=sql,
        table_ids=["table-orders", "table-customers"],
        column_ids=["column-amount", "column-id", "column-customer-id"],
    )
    result = await validate_query(
        draft,
        _context(),
        QueryIntent(query_type=QueryType.DETAIL, measure_quotes=["金额"]),
        dw_database="dw",
    )
    assert result.issues[0].code == "join_unsupported"


@pytest.mark.parametrize(
    ("sql", "sorts"),
    [
        (
            "SELECT o.amount FROM dw.orders o LIMIT 10",
            [SortIntent(quote="金额", direction="desc")],
        ),
        (
            "SELECT o.amount FROM dw.orders o ORDER BY o.amount ASC LIMIT 10",
            [SortIntent(quote="金额", direction="desc")],
        ),
    ],
)
async def test_ranking_requires_exact_sort(sql: str, sorts: list[SortIntent]) -> None:
    """排名查询必须按已绑定对象和用户方向排序。"""
    context = _context().model_copy(update={"bindings": {"金额": "column-amount"}})
    result = await validate_query(
        _draft(sql),
        context,
        QueryIntent(
            query_type=QueryType.RANKING, measure_quotes=["金额"], sorts=sorts, limit=10
        ),
        dw_database="dw",
    )
    assert result.issues[0].code == "sort_mismatch"


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
