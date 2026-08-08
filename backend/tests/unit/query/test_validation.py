"""只读 SQLGlot 门禁的行为测试。"""

import asyncio
import threading
from decimal import Decimal
from typing import Literal

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
    QueryAmbiguity,
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
                        PhysicalColumn(
                            id="column-code", name="code", data_type="VARCHAR(16)"
                        ),
                    ],
                ),
                PhysicalTable(
                    id="table-customers",
                    name="customers",
                    qualified_name="customers",
                    primary_key=["id"],
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


@pytest.mark.parametrize(
    "question", ["列出订单编号，订单金额", "列出订单编号,订单金额"]
)
def test_detail_result_fields_split_on_commas(question: str) -> None:
    """中英文逗号列举的明细字段必须逐项进入可信意图。"""
    with pytest.raises(ValueError, match="每个明细结果字段"):
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["订单编号"],
        ).validate_evidence([question])


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_english_sort_direction_cannot_be_omitted(direction: str) -> None:
    """英文排序方向必须反向覆盖到排序槽位。"""
    with pytest.raises(ValueError, match="排序必须完整映射"):
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["订单编号"],
        ).validate_evidence([f"订单编号 {direction}"])


async def test_text_filter_preserves_leading_zeroes() -> None:
    """文本标识符不得被数值规范化后丢失前导零。"""
    context = _context().model_copy(update={"bindings": {"编号": "column-code"}})
    result = await validate_query(
        QueryDraft(
            sql="SELECT o.code FROM dw.orders o WHERE o.code = :code",
            params={"code": "001"},
            table_ids=["table-orders"],
            column_ids=["column-code"],
        ),
        context,
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["编号"],
            filters=[
                FilterIntent(
                    column_quote="编号",
                    operator="eq",
                    operator_quote="是",
                    value_quotes=["001"],
                )
            ],
        ),
        dw_database="dw",
    )
    assert result.validated is not None


def test_intent_requires_all_supported_explicit_slots() -> None:
    """周期、等值过滤、排序、明细字段和分组动作都不能被缩水。"""
    with pytest.raises(ValueError, match="趋势形态"):
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="sum",
            aggregation_quote="合计",
            measure_quotes=["销售额"],
        ).validate_evidence(["每月销售额合计"])
    with pytest.raises(ValueError, match="每项过滤"):
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["订单编号"],
            filters=[
                FilterIntent(
                    column_quote="地区",
                    operator="eq",
                    operator_quote="是",
                    value_quotes=["华东"],
                    clause_quote="地区是华东",
                )
            ],
        ).validate_evidence(["列出地区是华东且状态是完成的订单编号"])
    with pytest.raises(ValueError, match="每项排序"):
        QueryIntent(
            query_type=QueryType.RANKING,
            measure_quotes=["订单编号"],
            sorts=[
                SortIntent(quote="销售额", direction="desc", direction_quote="降序")
            ],
            limit=10,
            limit_quote="前10条",
        ).validate_evidence(["销售额降序、下单时间升序的前10条订单编号"])
    with pytest.raises(ValueError, match="明细结果字段"):
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["订单编号"],
        ).validate_evidence(["列出订单编号和订单金额"])
    with pytest.raises(ValueError, match="动作证据"):
        QueryIntent(
            query_type=QueryType.COMPARISON,
            aggregation="sum",
            aggregation_quote="合计",
            measure_quotes=["销售额"],
            dimension_quotes=["销售额"],
        ).validate_evidence(["销售额合计"])


def test_detail_result_fields_remain_complete_before_filter_clause() -> None:
    """带过滤的明细请求也必须保留过滤子句前的全部结果字段。"""
    with pytest.raises(ValueError, match="明细结果字段"):
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["订单编号"],
            filters=[
                FilterIntent(
                    column_quote="地区",
                    operator="eq",
                    operator_quote="是",
                    value_quotes=["华东"],
                    clause_quote="地区是华东",
                )
            ],
        ).validate_evidence(["列出订单编号和订单金额，地区是华东"])


def test_detail_result_fields_remain_complete_after_filter_clause() -> None:
    """过滤在前时仍必须逐项覆盖其后的全部明细结果字段。"""
    with pytest.raises(ValueError, match="每个明细结果字段"):
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["订单编号"],
            filters=[
                FilterIntent(
                    column_quote="地区",
                    operator="eq",
                    operator_quote="是",
                    value_quotes=["华东"],
                    clause_quote="地区是华东",
                )
            ],
        ).validate_evidence(["列出地区是华东的订单编号和订单金额"])


def test_explicit_year_range_cannot_be_omitted_from_intent() -> None:
    """完整原文中的显式年份必须形成可信时间过滤契约。"""
    with pytest.raises(ValueError, match="时间范围"):
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="sum",
            aggregation_quote="合计",
            measure_quotes=["销售额"],
        ).validate_evidence(["查询2025年销售额合计"])


def test_explicit_year_without_time_column_can_reach_clarification() -> None:
    """显式年份缺少时间字段时可携带 time ambiguity 进入 Meta 澄清。"""
    intent = QueryIntent(
        query_type=QueryType.AGGREGATE,
        aggregation="sum",
        aggregation_quote="合计",
        measure_quotes=["销售额"],
        time_quote="2025年",
        ambiguities=[
            query_domain.QueryAmbiguity(
                slot="time", quote="2025年", question="请明确时间字段？"
            )
        ],
    )

    assert intent.validate_evidence(["查询2025年销售额合计"]) is None


def test_or_is_rejected_even_when_model_extracts_only_one_branch() -> None:
    """未建模 OR 不能通过只抽取一个过滤分支绕过。"""
    with pytest.raises(ValueError, match="OR"):
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["订单编号"],
            filters=[
                FilterIntent(
                    column_quote="地区",
                    operator="eq",
                    operator_quote="是",
                    value_quotes=["华东"],
                    clause_quote="地区是华东",
                )
            ],
        ).validate_evidence(["查询地区是华东或状态是完成的订单编号"])


@pytest.mark.parametrize("unit", ["条", "笔", "个"])
def test_result_count_unit_cannot_be_omitted_from_ranking(unit: str) -> None:
    """明确数量单位的截断请求必须形成可信排名与 LIMIT。"""
    with pytest.raises(ValueError, match="Top-N"):
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["记录"],
            sorts=[
                SortIntent(
                    quote="销售额", direction="desc", direction_quote="降序"
                )
            ],
        ).validate_evidence([f"销售额降序的10{unit}记录"])


@pytest.mark.parametrize("marker", ["个数", "最大", "最高", "最小", "最低"])
def test_all_supported_aggregation_actions_must_enter_intent(marker: str) -> None:
    """计数与极值动作不得被降级为明细意图。"""
    with pytest.raises(ValueError, match="聚合"):
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["订单编号"],
        ).validate_evidence([f"查询订单编号{marker}"])


async def test_date_only_equality_on_timestamp_fails_closed() -> None:
    """时间戳列的自然日不能被错误表达为午夜等值比较。"""
    context = _context()
    orders = context.physical_schema.tables[0]
    timestamp_column = next(
        column for column in orders.columns if column.id == "column-created-at"
    ).model_copy(update={"data_type": "TIMESTAMP"})
    columns = [
        timestamp_column if column.id == timestamp_column.id else column
        for column in orders.columns
    ]
    context = context.model_copy(
        update={
            "physical_schema": context.physical_schema.model_copy(
                update={
                    "tables": [
                        orders.model_copy(update={"columns": columns}),
                        *context.physical_schema.tables[1:],
                    ]
                }
            ),
            "bindings": {"下单日期": "column-created-at"},
        }
    )
    result = await validate_query(
        QueryDraft(
            sql="SELECT o.created_at FROM dw.orders o WHERE o.created_at = :day",
            params={"day": "2026-08-01"},
            table_ids=["table-orders"],
            column_ids=["column-created-at"],
        ),
        context,
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["下单日期"],
            filters=[
                FilterIntent(
                    column_quote="下单日期",
                    operator="eq",
                    operator_quote="是",
                    value_quotes=["2026年8月1日"],
                )
            ],
        ),
        dw_database="dw",
    )

    assert [issue.code for issue in result.issues] == ["filter_value_type_mismatch"]


@pytest.mark.parametrize("connector", ["和", "以及"])
def test_all_conjunctive_filter_clauses_must_enter_intent(connector: str) -> None:
    """和、以及连接的每项正向过滤都不得被模型省略。"""
    with pytest.raises(ValueError, match="每项过滤条件"):
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["订单编号"],
            filters=[
                FilterIntent(
                    column_quote="地区",
                    operator="eq",
                    operator_quote="是",
                    value_quotes=["华东"],
                    clause_quote="地区是华东",
                )
            ],
        ).validate_evidence([f"列出地区是华东{connector}状态是完成的订单编号"])


def test_top_n_sort_ambiguity_can_reach_clarification() -> None:
    """缺少排序键的 Top-N 可携带排序歧义进入 Meta 澄清。"""
    intent = QueryIntent(
        query_type=QueryType.RANKING,
        query_type_quote="前10个",
        measure_quotes=["订单"],
        limit=10,
        limit_quote="前10个",
        ambiguities=[
            query_domain.QueryAmbiguity(
                slot="sort", quote="前10个", question="请明确排序依据？"
            )
        ],
    )

    assert intent.validate_evidence(["列出前10个订单"]) is None


async def test_join_target_must_be_proven_unique() -> None:
    """目标列未由主键证明唯一时不能授权聚合 JOIN。"""
    context = _context()
    customers = context.physical_schema.tables[1].model_copy(update={"primary_key": []})
    context = context.model_copy(
        update={
            "physical_schema": context.physical_schema.model_copy(
                update={"tables": [context.physical_schema.tables[0], customers]}
            ),
            "relationships_authoritative": True,
            "bindings": {"金额": "column-amount", "客户": "table-customers"},
        }
    )
    result = await validate_query(
        QueryDraft(
            sql="SELECT o.amount FROM dw.orders o JOIN dw.customers c ON o.id = c.id",
            table_ids=["table-orders", "table-customers"],
            column_ids=["column-amount", "column-id", "column-customer-id"],
        ),
        context,
        QueryIntent(query_type=QueryType.DETAIL, measure_quotes=["金额"]),
        dw_database="dw",
    )

    assert result.issues[0].code == "join_unsupported"


async def test_join_rejects_unmodeled_boolean_leaf() -> None:
    """JOIN ON 的每个叶子都必须属于获授权外键等式。"""
    context = _context().model_copy(
        update={
            "relationships_authoritative": True,
            "bindings": {"金额": "column-amount", "客户": "table-customers"},
        }
    )
    result = await validate_query(
        QueryDraft(
            sql=(
                "SELECT o.amount FROM dw.orders o JOIN dw.customers c "
                "ON o.id = c.id AND c.id"
            ),
            table_ids=["table-orders", "table-customers"],
            column_ids=[
                "column-amount",
                "column-customer-id",
                "column-id",
            ],
        ),
        context,
        QueryIntent(query_type=QueryType.DETAIL, measure_quotes=["金额"]),
        dw_database="dw",
    )

    assert result.issues[0].code == "join_unsupported"


async def test_swapped_detail_aliases_fail_closed() -> None:
    """公开结果列标签不能交换两个权威字段的业务身份。"""
    context = _context().model_copy(
        update={"bindings": {"编号": "column-id", "金额": "column-amount"}}
    )
    result = await validate_query(
        QueryDraft(
            sql="SELECT o.id AS amount, o.amount AS id FROM dw.orders o",
            table_ids=["table-orders"],
            column_ids=["column-id", "column-amount"],
        ),
        context,
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["编号", "金额"],
        ),
        dw_database="dw",
    )
    assert result.issues[0].code == "projection_alias_mismatch"


def test_aggregate_question_is_not_mistaken_for_equality_filter() -> None:
    """“是多少”疑问结构不得被完整性门禁误判为等值过滤。"""
    intent = QueryIntent(
        query_type=QueryType.AGGREGATE,
        query_type_quote="合计",
        aggregation="sum",
        aggregation_quote="合计",
        measure_quotes=["销售额"],
    )

    assert intent.validate_evidence(["销售额合计是多少"]) is None


def test_explicit_equality_cannot_be_omitted_from_intent() -> None:
    """“是”表达的等值条件必须进入过滤槽位。"""
    with pytest.raises(ValueError, match="过滤条件"):
        QueryIntent(
            query_type=QueryType.DETAIL, measure_quotes=["订单编号"]
        ).validate_evidence(["列出地区是华东的订单编号"])


def test_unmodeled_negated_filter_cannot_be_omitted_from_intent() -> None:
    """未建模否定过滤即使被模型省略也必须在规划前失败关闭。"""
    with pytest.raises(ValueError, match="否定语义"):
        QueryIntent(
            query_type=QueryType.DETAIL, measure_quotes=["订单编号"]
        ).validate_evidence(["列出地区不在华东的订单编号"])


@pytest.mark.parametrize(
    ("sql", "issue"),
    [
        (
            "WITH scoped AS (SELECT o.amount FROM dw.orders o GROUP BY o.amount) "
            "SELECT amount FROM scoped",
            "derived_lineage_unsupported",
        ),
        (
            "SELECT SUM(o.amount) FROM dw.orders o HAVING amount > :minimum",
            "predicate_mismatch",
        ),
        (
            "SELECT o.amount FROM dw.orders o GROUP BY o.amount WITH ROLLUP",
            "group_modifier_forbidden",
        ),
    ],
)
async def test_unmodeled_cardinality_and_filter_stages_fail_closed(
    sql: str, issue: str
) -> None:
    """派生分组、HAVING 与分组修饰符不能冒充可信结果契约。"""
    draft = _draft(sql, params={"minimum": 10} if ":minimum" in sql else {})
    result = await validate_query(
        draft,
        _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["金额"],
            filters=(
                [
                    FilterIntent(
                        column_quote="金额",
                        operator="gt",
                        value_quotes=["10"],
                    )
                ]
                if ":minimum" in sql
                else []
            ),
        ),
        dw_database="dw",
    )
    assert result.issues[0].code == issue


async def test_filter_uses_typed_normalized_value() -> None:
    """千位分隔原文证据可绑定确定性解析后的数值。"""
    result = await validate_query(
        _draft(
            "SELECT o.amount FROM dw.orders o WHERE o.amount > :minimum",
            params={"minimum": 1000},
        ),
        _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["金额"],
            filters=[
                FilterIntent(
                    column_quote="金额",
                    operator="gt",
                    value_quotes=["1,000"],
                )
            ],
        ),
        dw_database="dw",
    )
    assert result.validated is not None


async def test_detail_requires_bound_projection_and_decimal_stays_exact() -> None:
    """明细必须绑定结果列，高精度 DECIMAL 参数不得转为 float。"""
    context = _context().model_copy(update={"bindings": {"金额": "column-amount"}})
    intent = QueryIntent(
        query_type=QueryType.DETAIL,
        measure_quotes=["金额"],
        filters=[
            FilterIntent(
                column_quote="金额",
                operator="gt",
                value_quotes=["12345678901234567890.12"],
            )
        ],
    )
    result = await validate_query(
        _draft(
            "SELECT o.amount FROM dw.orders o WHERE o.amount > :minimum",
            params={"minimum": Decimal("12345678901234567890.12")},
        ),
        context,
        intent,
        dw_database="dw",
    )
    assert result.validated is not None

    unbound = await validate_query(
        QueryDraft(
            sql="SELECT o.id FROM dw.orders o WHERE o.amount > :minimum",
            params={"minimum": 10},
            table_ids=["table-orders"],
            column_ids=["column-id", "column-amount"],
        ),
        _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
        QueryIntent(
            query_type=QueryType.DETAIL,
            filters=[
                FilterIntent(column_quote="金额", operator="gt", value_quotes=["10"])
            ],
        ),
        dw_database="dw",
    )
    assert unbound.issues[0].code == "projection_mismatch"


async def test_rejects_partition_unbound_group_and_wrong_aggregate_sort() -> None:
    """分区、未绑定分组及不同聚合排序均不得通过。"""
    context = _context().model_copy(
        update={"bindings": {"地区": "column-id", "金额": "column-amount"}}
    )
    intent = QueryIntent(
        query_type=QueryType.COMPARISON,
        aggregation="sum",
        aggregation_quote="总和",
        measure_quotes=["金额"],
        dimension_quotes=["地区"],
        sorts=[SortIntent(quote="金额", direction="desc", direction_quote="最高")],
    )
    cases = [
        QueryDraft(
            sql="SELECT SUM(o.amount) FROM dw.orders PARTITION (p1) o",
            table_ids=["table-orders"],
            column_ids=["column-amount"],
        ),
        QueryDraft(
            sql=(
                "SELECT o.id, YEAR(o.created_at), SUM(o.amount) FROM dw.orders o "
                "GROUP BY o.id, YEAR(o.created_at) ORDER BY SUM(o.amount) DESC"
            ),
            table_ids=["table-orders"],
            column_ids=["column-id", "column-created-at", "column-amount"],
        ),
        QueryDraft(
            sql=(
                "SELECT o.id, SUM(o.amount) FROM dw.orders o GROUP BY o.id "
                "ORDER BY MAX(o.amount) DESC"
            ),
            table_ids=["table-orders"],
            column_ids=["column-id", "column-amount"],
        ),
    ]
    issues = []
    for draft in cases:
        result = await validate_query(draft, context, intent, dw_database="dw")
        issues.append(result.issues[0].code)
    assert issues == ["table_modifier_forbidden", "group_mismatch", "sort_mismatch"]


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


def test_incomplete_trend_can_reach_single_slot_clarification() -> None:
    """趋势缺少粒度或聚合时，可由对应歧义进入 Meta 单项澄清。"""
    intent = QueryIntent(
        query_type=QueryType.TREND,
        query_type_quote="趋势",
        measure_quotes=["销售额"],
        ambiguities=[
            QueryAmbiguity(slot="time", quote="趋势", question="请明确时间粒度？"),
            QueryAmbiguity(
                slot="measure", quote="销售额", question="请明确聚合口径？"
            ),
        ],
    )

    assert intent.validate_evidence(["查看销售额趋势"]) is None


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


@pytest.mark.parametrize("side", ["LEFT", "RIGHT"])
async def test_outer_join_is_rejected_without_trusted_semantics(side: str) -> None:
    """当前只有内连接契约，外连接不得引入未匹配行。"""
    result = await validate_query(
        QueryDraft(
            sql=(
                f"SELECT o.amount FROM dw.orders o {side} JOIN dw.customers c "
                "ON o.id = c.id"
            ),
            table_ids=["table-orders", "table-customers"],
            column_ids=["column-id", "column-customer-id", "column-amount"],
        ),
        _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
        QueryIntent(query_type=QueryType.DETAIL, measure_quotes=["金额"]),
        dw_database="dw",
    )
    assert result.issues[0].code == "join_forbidden"


def test_explicit_trend_cannot_be_downgraded_to_detail() -> None:
    """完整用户消息中的趋势标记必须被意图覆盖。"""
    with pytest.raises(ValueError, match="趋势形态"):
        QueryIntent(
            query_type=QueryType.DETAIL,
            query_type_quote="列出",
            measure_quotes=["销售额"],
        ).validate_evidence(["列出每月销售额趋势"])


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
        _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
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


@pytest.mark.parametrize(
    "sql",
    [
        "WITH scoped AS (SELECT SUM(o.amount) AS amount FROM dw.orders AS o) "
        "SELECT amount FROM scoped",
        "WITH scoped AS (SELECT o.amount * 0 AS amount FROM dw.orders AS o) "
        "SELECT amount FROM scoped",
        "WITH scoped AS (SELECT o.amount FROM dw.orders AS o UNION ALL "
        "SELECT o.amount FROM dw.orders AS o) SELECT amount FROM scoped",
    ],
)
async def test_derived_outputs_require_direct_column_lineage(sql: str) -> None:
    """派生输出只有直接透传物理列时才能参与结果形态验证。"""
    result = await validate_query(
        _draft(sql),
        _context(),
        QueryIntent(query_type=QueryType.DETAIL, measure_quotes=["金额"]),
        dw_database="dw",
    )

    assert result.issues[0].code == "derived_lineage_unsupported"


async def test_unbound_fk_join_is_rejected() -> None:
    """即使 JOIN 使用合法外键，也不能引入未受意图绑定的额外表。"""
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

    assert result.issues[0].code == "table_binding_mismatch"


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


@pytest.mark.parametrize(
    "predicate",
    [
        "EXISTS (SELECT c.id FROM dw.customers c)",
        "o.amount BETWEEN :minimum AND :maximum",
    ],
)
async def test_unmodeled_predicate_nodes_fail_closed(predicate: str) -> None:
    """WHERE/HAVING 只允许可逐项映射到可信意图的原子谓词。"""
    params: dict[str, QueryParameter] = (
        {"minimum": 1, "maximum": 10} if "BETWEEN" in predicate else {}
    )
    draft = _draft(f"SELECT o.amount FROM dw.orders o WHERE {predicate}", params=params)
    if "EXISTS" in predicate:
        draft = draft.model_copy(
            update={
                "table_ids": ["table-orders", "table-customers"],
                "column_ids": ["column-amount", "column-customer-id"],
            }
        )
    result = await validate_query(
        draft,
        _context(),
        QueryIntent(query_type=QueryType.DETAIL, measure_quotes=["金额"]),
        dw_database="dw",
    )
    assert result.issues[0].code == "predicate_mismatch"


@pytest.mark.parametrize(
    ("operator", "operator_quote"),
    [("lt", "大于"), ("lte", "至少"), ("ne", "等于")],
)
def test_filter_operator_requires_matching_user_evidence(
    operator: Literal["lt", "lte", "ne"], operator_quote: str
) -> None:
    """过滤方向必须由用户逐字原文证明，不能只信任模型枚举。"""
    intent = QueryIntent(
        query_type=QueryType.DETAIL,
        filters=[
            FilterIntent(
                column_quote="金额",
                operator=operator,
                operator_quote=operator_quote,
                value_quotes=["10"],
            )
        ],
    )
    with pytest.raises(ValueError, match="过滤操作"):
        intent.validate_evidence([f"金额{operator_quote}10"])


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT o.amount, o.id FROM dw.orders o",
        "SELECT o.amount * 0 FROM dw.orders o",
    ],
)
async def test_detail_projection_exactly_matches_bound_results(sql: str) -> None:
    """明细顶层投影不得附加字段或篡改绑定字段。"""
    draft = _draft(sql)
    if "o.id" in sql:
        draft = draft.model_copy(update={"column_ids": ["column-amount", "column-id"]})
    result = await validate_query(
        draft,
        _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
        QueryIntent(query_type=QueryType.DETAIL, measure_quotes=["金额"]),
        dw_database="dw",
    )
    assert result.issues[0].code == "projection_mismatch"


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


def test_distinct_user_intent_cannot_degrade_to_plain_count() -> None:
    """显式去重语义在尚无契约时必须在规划前失败关闭。"""
    with pytest.raises(ValueError, match="去重"):
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            query_type_quote="数量",
            aggregation="count",
            aggregation_quote="数量",
            measure_quotes=["客户"],
        ).validate_evidence(["查询不同客户的数量"])


async def test_time_bucket_requires_temporal_physical_column() -> None:
    """文本日期字段不能依赖 MySQL 隐式转换参与趋势分桶。"""
    context = _context().model_copy(deep=True)
    context.physical_schema.tables[0].columns[2].data_type = "VARCHAR(64)"
    context.bindings = {"金额": "column-amount", "日期": "column-created-at"}
    result = await validate_query(
        QueryDraft(
            sql=(
                "SELECT DATE(o.created_at), SUM(o.amount) FROM dw.orders o "
                "GROUP BY DATE(o.created_at)"
            ),
            table_ids=["table-orders"],
            column_ids=["column-created-at", "column-amount"],
        ),
        context,
        QueryIntent(
            query_type=QueryType.TREND,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["金额"],
            time_column_quote="日期",
            grain="day",
            grain_quote="按日",
        ),
        dw_database="dw",
    )
    assert result.issues[0].code == "time_type_mismatch"


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


async def test_aggregate_ranking_without_dimension_is_rejected() -> None:
    """没有分组维度的 Top-N 不得用单个聚合值冒充排名明细。"""
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

    assert result.issues[0].code == "query_shape_mismatch"


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


async def test_join_rejects_sibling_fact_fanout_from_measure_grain() -> None:
    """度量只能沿多对一方向扩展，不能再从共同父表进入另一子表。"""
    schema = PhysicalSchema(
        source="erp",
        canonical_ddl="schema",
        ddl_hash="ddl",
        schema_fingerprint="schema",
        tables=[
            PhysicalTable(
                id="table-orders",
                name="orders",
                qualified_name="orders",
                columns=[
                    PhysicalColumn(
                        id="order-customer", name="customer_id", data_type="BIGINT"
                    ),
                    PhysicalColumn(
                        id="order-amount", name="amount", data_type="DECIMAL(10,2)"
                    ),
                ],
            ),
            PhysicalTable(
                id="table-customers",
                name="customers",
                qualified_name="customers",
                primary_key=["id"],
                columns=[
                    PhysicalColumn(id="customer-id", name="id", data_type="BIGINT"),
                    PhysicalColumn(
                        id="customer-region", name="region", data_type="VARCHAR(20)"
                    ),
                ],
            ),
            PhysicalTable(
                id="table-refunds",
                name="refunds",
                qualified_name="refunds",
                columns=[
                    PhysicalColumn(
                        id="refund-customer", name="customer_id", data_type="BIGINT"
                    ),
                    PhysicalColumn(
                        id="refund-reason", name="reason", data_type="VARCHAR(20)"
                    ),
                ],
            ),
        ],
        relationships=[
            PhysicalRelationship(
                source_table_id="table-orders",
                source_column_id="order-customer",
                target_table="customers",
                target_column="id",
                constraint_id="orders-customer",
            ),
            PhysicalRelationship(
                source_table_id="table-refunds",
                source_column_id="refund-customer",
                target_table="customers",
                target_column="id",
                constraint_id="refunds-customer",
            ),
        ],
    )
    context = QueryContext(
        physical_schema=schema,
        relationships_authoritative=True,
        bindings={
            "金额": "order-amount",
            "地区": "customer-region",
            "退款原因": "refund-reason",
        },
    )
    draft = QueryDraft(
        sql=(
            "SELECT c.region, r.reason, SUM(o.amount) FROM dw.orders o "
            "JOIN dw.customers c ON o.customer_id = c.id "
            "JOIN dw.refunds r ON r.customer_id = c.id "
            "GROUP BY c.region, r.reason"
        ),
        table_ids=["table-orders", "table-customers", "table-refunds"],
        column_ids=[
            "order-customer",
            "order-amount",
            "customer-id",
            "customer-region",
            "refund-customer",
            "refund-reason",
        ],
    )
    result = await validate_query(
        draft,
        context,
        QueryIntent(
            query_type=QueryType.COMPARISON,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["金额"],
            dimension_quotes=["地区", "退款原因"],
        ),
        dw_database="dw",
    )
    assert result.issues[0].code == "join_cardinality_unsupported"


async def test_join_cannot_merge_two_independent_foreign_keys() -> None:
    """单个 JOIN 必须精确匹配一个外键约束，而非合并两个约束。"""
    schema = _context().physical_schema.model_copy(
        update={
            "relationships": [
                PhysicalRelationship(
                    source_table_id="table-orders",
                    source_column_id="column-id",
                    target_table="CUSTOMERS",
                    target_column="ID",
                    constraint_id="fk-created",
                ),
                PhysicalRelationship(
                    source_table_id="table-orders",
                    source_column_id="column-amount",
                    target_table="customers",
                    target_column="id",
                    constraint_id="fk-approved",
                ),
            ]
        }
    )
    result = await validate_query(
        QueryDraft(
            sql=(
                "SELECT o.amount FROM dw.orders o JOIN dw.customers c "
                "ON o.id = c.id AND o.amount = c.id"
            ),
            table_ids=["table-orders", "table-customers"],
            column_ids=["column-amount", "column-id", "column-customer-id"],
        ),
        QueryContext(physical_schema=schema),
        QueryIntent(query_type=QueryType.DETAIL, measure_quotes=["金额"]),
        dw_database="dw",
    )
    assert result.issues[0].code == "join_unsupported"

    valid = await validate_query(
        QueryDraft(
            sql="SELECT o.amount FROM dw.orders o JOIN dw.customers c ON o.id = c.id",
            table_ids=["table-orders", "table-customers"],
            column_ids=["column-amount", "column-id", "column-customer-id"],
        ),
        QueryContext(
            physical_schema=schema,
            bindings={"金额": "column-amount", "客户表": "table-customers"},
        ),
        QueryIntent(query_type=QueryType.DETAIL, measure_quotes=["金额"]),
        dw_database="dw",
    )
    assert valid.validated is not None


async def test_many_to_one_join_allows_parent_dimension_for_child_measure() -> None:
    """子表度量按父表维度分组不会放大子表结果粒度。"""
    context = _context()
    customers = context.physical_schema.tables[1].model_copy(
        update={
            "columns": [
                *context.physical_schema.tables[1].columns,
                PhysicalColumn(
                    id="column-customer-region", name="region", data_type="VARCHAR(32)"
                ),
            ]
        }
    )
    context = context.model_copy(
        update={
            "physical_schema": context.physical_schema.model_copy(
                update={"tables": [context.physical_schema.tables[0], customers]}
            ),
            "bindings": {"金额": "column-amount", "地区": "column-customer-region"},
        }
    )
    result = await validate_query(
        QueryDraft(
            sql=(
                "SELECT c.region, SUM(o.amount) FROM dw.orders o "
                "JOIN dw.customers c ON o.id = c.id GROUP BY c.region"
            ),
            table_ids=["table-orders", "table-customers"],
            column_ids=[
                "column-id",
                "column-customer-id",
                "column-customer-region",
                "column-amount",
            ],
        ),
        context,
        QueryIntent(
            query_type=QueryType.COMPARISON,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["金额"],
            dimension_quotes=["地区"],
        ),
        dw_database="dw",
    )
    assert result.validated is not None


async def test_table_count_rejects_joining_one_to_many_child() -> None:
    """表绑定的计数主体不能沿一对多方向连接子表。"""
    context = _context().model_copy(
        update={
            "relationships_authoritative": True,
            "bindings": {"客户": "table-customers", "金额": "column-amount"},
        }
    )
    result = await validate_query(
        QueryDraft(
            sql=(
                "SELECT o.amount, COUNT(*) AS total FROM dw.customers c "
                "JOIN dw.orders o ON o.id = c.id GROUP BY o.amount"
            ),
            table_ids=["table-customers", "table-orders"],
            column_ids=["column-customer-id", "column-id", "column-amount"],
        ),
        context,
        QueryIntent(
            query_type=QueryType.COMPARISON,
            query_type_quote="按",
            aggregation="count",
            aggregation_quote="数量",
            measure_quotes=["客户"],
            dimension_quotes=["金额"],
        ),
        dw_database="dw",
    )
    assert result.issues[0].code == "join_cardinality_unsupported"


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
            _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
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


async def test_nested_limit_and_transformed_sort_fail_closed() -> None:
    """嵌套截断和变换排序均不能冒充可信 Top-N。"""
    context = _context().model_copy(update={"bindings": {"金额": "column-amount"}})
    nested = await validate_query(
        _draft(
            "SELECT SUM(scoped.amount) FROM "
            "(SELECT o.amount FROM dw.orders o LIMIT 1) scoped"
        ),
        context,
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["金额"],
        ),
        dw_database="dw",
    )
    assert nested.issues[0].code == "nested_limit_forbidden"
    transformed = await validate_query(
        _draft("SELECT o.amount FROM dw.orders o ORDER BY o.amount * 0 DESC LIMIT 10"),
        context,
        QueryIntent(
            query_type=QueryType.RANKING,
            measure_quotes=["金额"],
            sorts=[SortIntent(quote="金额", direction="desc")],
            limit=10,
        ),
        dw_database="dw",
    )
    assert transformed.issues[0].code == "sort_mismatch"


async def test_scalar_aggregate_rejects_grouping_and_transformed_operand() -> None:
    """标量汇总只能输出直接作用于绑定度量的单个聚合。"""
    context = _context().model_copy(update={"bindings": {"金额": "column-amount"}})
    intent = QueryIntent(
        query_type=QueryType.AGGREGATE,
        aggregation="sum",
        aggregation_quote="总和",
        measure_quotes=["金额"],
    )
    grouped = await validate_query(
        QueryDraft(
            sql="SELECT o.id, SUM(o.amount) FROM dw.orders o GROUP BY o.id",
            table_ids=["table-orders"],
            column_ids=["column-id", "column-amount"],
        ),
        context,
        intent,
        dw_database="dw",
    )
    assert grouped.issues[0].code == "query_shape_mismatch"
    transformed = await validate_query(
        _draft("SELECT SUM(o.amount * 0) FROM dw.orders o"),
        context,
        intent,
        dw_database="dw",
    )
    assert transformed.issues[0].code == "aggregation_operand_mismatch"


def test_intent_requires_sort_and_grain_evidence() -> None:
    """排序方向和时间粒度枚举必须由用户原文确定性证明。"""
    with pytest.raises(ValueError, match="排序方向"):
        QueryIntent(
            query_type=QueryType.RANKING,
            sorts=[
                SortIntent(quote="金额", direction="desc", direction_quote="从低到高")
            ],
        ).validate_evidence(["金额从低到高"])
    with pytest.raises(ValueError, match="时间粒度"):
        QueryIntent(
            query_type=QueryType.TREND,
            time_quote="2025年",
            time_column_quote="日期",
            time_filter=FilterIntent(
                column_quote="日期",
                operator="gte",
                operator_quote="大于等于",
                value_quotes=["2025"],
            ),
            grain="day",
            grain_quote="按月",
        ).validate_evidence(["按月查看日期大于等于2025年"])


def test_intent_normalizes_or_equal_and_requires_time_clause_evidence() -> None:
    """或等于边界必须规范化，时间完整子句也必须逐字存在。"""
    QueryIntent(
        query_type=QueryType.DETAIL,
        measure_quotes=["金额"],
        filters=[
            FilterIntent(
                column_quote="金额",
                operator="gte",
                operator_quote="大于或等于",
                value_quotes=["10"],
            )
        ],
    ).validate_evidence(["金额大于或等于10"])
    with pytest.raises(ValueError, match="逐字"):
        QueryIntent(
            query_type=QueryType.TREND,
            time_quote="今年",
            time_column_quote="日期",
            time_filter=FilterIntent(
                column_quote="日期",
                operator="gte",
                operator_quote="大于等于",
                value_quotes=["2026-01-01"],
                clause_quote="日期大于等于2026-01-01",
            ),
            grain="month",
            grain_quote="按月",
        ).validate_evidence(["今年按月查看日期"])


def test_intent_rejects_ambiguous_grain_evidence() -> None:
    """范围单位和分组单位同时出现时不得由模型任选一个粒度。"""
    with pytest.raises(ValueError, match="时间粒度"):
        QueryIntent(
            query_type=QueryType.TREND,
            measure_quotes=["金额"],
            time_column_quote="日期",
            grain="day",
            grain_quote="最近7天按月",
        ).validate_evidence(["最近7天按月查看日期和金额"])


def test_intent_requires_grouping_and_limit_semantics_without_false_or() -> None:
    """显式维度和截断动作不得省略，复合不等式中的“或”不是布尔 OR。"""
    with pytest.raises(ValueError, match="分组维度"):
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="sum",
            aggregation_quote="合计",
            measure_quotes=["销售额"],
        ).validate_evidence(["按地区统计销售额合计"])
    with pytest.raises(ValueError, match="结果截断语义"):
        QueryIntent(
            query_type=QueryType.RANKING,
            measure_quotes=["订单编号"],
            sorts=[
                SortIntent(quote="订单编号", direction="desc", direction_quote="降序")
            ],
            limit=2026,
            limit_quote="2026",
        ).validate_evidence(["2026年订单编号降序"])
    QueryIntent(
        query_type=QueryType.DETAIL,
        measure_quotes=["订单编号"],
        filters=[
            FilterIntent(
                column_quote="金额",
                operator="gte",
                operator_quote="大于或等于",
                value_quotes=["100"],
                clause_quote="金额大于或等于100",
            ),
            FilterIntent(
                column_quote="库存",
                operator="lt",
                operator_quote="小于",
                value_quotes=["10"],
                clause_quote="库存小于10",
            ),
        ],
    ).validate_evidence(["列出金额大于或等于100且库存小于10的订单编号"])


def test_intent_requires_every_explicit_filter_and_grouping_dimension() -> None:
    """多项过滤与分组维度不能只抽取其中一项。"""
    with pytest.raises(ValueError, match="每项过滤"):
        QueryIntent(
            query_type=QueryType.DETAIL,
            measure_quotes=["订单编号"],
            filters=[
                FilterIntent(
                    column_quote="金额",
                    operator="gt",
                    operator_quote="大于",
                    value_quotes=["100"],
                    clause_quote="金额大于100",
                )
            ],
        ).validate_evidence(["列出金额大于100且数量小于10的订单编号"])
    with pytest.raises(ValueError, match="分组维度"):
        QueryIntent(
            query_type=QueryType.COMPARISON,
            aggregation="sum",
            aggregation_quote="合计",
            measure_quotes=["销售额"],
            dimension_quotes=["地区"],
        ).validate_evidence(["按地区和产品统计销售额合计"])


@pytest.mark.parametrize("separator", ["，", ","])
def test_intent_requires_every_comma_separated_grouping_dimension(
    separator: str,
) -> None:
    """逗号列举的分组维度必须逐项进入可信意图。"""
    with pytest.raises(ValueError, match="分组维度"):
        QueryIntent(
            query_type=QueryType.COMPARISON,
            aggregation="sum",
            aggregation_quote="合计",
            measure_quotes=["销售额"],
            dimension_quotes=["地区"],
        ).validate_evidence([f"按地区{separator}状态统计销售额合计"])


def test_intent_requires_every_symbol_filter_clause() -> None:
    """符号操作符连接的多项过滤不能只抽取首项。"""
    with pytest.raises(ValueError, match="每项过滤"):
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="count",
            aggregation_quote="数量",
            measure_quotes=["订单"],
            filters=[
                FilterIntent(
                    column_quote="地区",
                    operator="eq",
                    operator_quote="=",
                    value_quotes=["华东"],
                    clause_quote="地区=华东",
                )
            ],
        ).validate_evidence(["地区=华东且状态=完成的订单数量"])


def test_sort_and_field_words_do_not_invent_aggregation() -> None:
    """排序方向和字段名称中的聚合词不会强制创建聚合槽位。"""
    QueryIntent(
        query_type=QueryType.RANKING,
        measure_quotes=["订单编号"],
        sorts=[
            SortIntent(
                quote="销售额",
                direction="desc",
                direction_quote="最高",
            )
        ],
        limit=10,
        limit_quote="前10条",
    ).validate_evidence(["销售额最高的前10条订单编号"])
    QueryIntent(
        query_type=QueryType.DETAIL,
        measure_quotes=["订单编号", "订单数量"],
    ).validate_evidence(["列出订单编号和订单数量"])


def test_different_dimension_is_not_distinct_intent() -> None:
    """“不同地区”表示分组维度，而不是未建模 DISTINCT。"""
    QueryIntent(
        query_type=QueryType.COMPARISON,
        query_type_quote="比较",
        aggregation="sum",
        aggregation_quote="合计",
        measure_quotes=["销售额"],
        dimension_quotes=["地区"],
    ).validate_evidence(["比较不同地区的销售额合计"])


def test_intent_rejects_negated_positive_operator_and_inconsistent_shape() -> None:
    """未建模否定词和与槽位矛盾的结果形态必须失败关闭。"""
    with pytest.raises(ValueError, match="否定语义"):
        QueryIntent(
            query_type=QueryType.DETAIL,
            filters=[
                FilterIntent(
                    column_quote="地区",
                    operator="contains",
                    operator_quote="不包含",
                    value_quotes=["华东"],
                )
            ],
        ).validate_evidence(["地区不包含华东"])
    with pytest.raises(ValueError, match="排名意图|查询形态"):
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="max",
            aggregation_quote="最高",
            measure_quotes=["金额"],
            limit=10,
            limit_quote="10笔",
        ).validate_evidence(["列出金额最高的10笔订单"])


async def test_aggregate_and_grouped_projections_reject_outer_transformations() -> None:
    """可信聚合和维度不能在顶层投影中再被算术篡改。"""
    aggregate_context = _context().model_copy(
        update={"bindings": {"金额": "column-amount"}}
    )
    aggregate = await validate_query(
        _draft("SELECT SUM(o.amount) * 0 FROM dw.orders o"),
        aggregate_context,
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["金额"],
        ),
        dw_database="dw",
    )
    assert aggregate.issues[0].code == "projection_mismatch"

    comparison = await validate_query(
        QueryDraft(
            sql="SELECT o.id * 0, SUM(o.amount) FROM dw.orders o GROUP BY o.id",
            table_ids=["table-orders"],
            column_ids=["column-id", "column-amount"],
        ),
        aggregate_context.model_copy(
            update={"bindings": {"订单": "column-id", "金额": "column-amount"}}
        ),
        QueryIntent(
            query_type=QueryType.COMPARISON,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["金额"],
            dimension_quotes=["订单"],
        ),
        dw_database="dw",
    )
    assert comparison.issues[0].code == "projection_mismatch"


async def test_time_bucket_requires_exact_ast_shape() -> None:
    """时间桶不能只因子树包含正确函数和字段就被接受。"""
    result = await validate_query(
        QueryDraft(
            sql=(
                "SELECT DATE(o.created_at) * 0, SUM(o.amount) FROM dw.orders o "
                "WHERE o.created_at >= :start GROUP BY DATE(o.created_at) * 0"
            ),
            params={"start": "2025-01-01"},
            table_ids=["table-orders"],
            column_ids=["column-created-at", "column-amount"],
        ),
        _context().model_copy(
            update={"bindings": {"金额": "column-amount", "日期": "column-created-at"}}
        ),
        QueryIntent(
            query_type=QueryType.TREND,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["金额"],
            time_quote="2025年后",
            time_column_quote="日期",
            time_filter=FilterIntent(
                column_quote="日期",
                operator="gte",
                operator_quote="大于等于",
                value_quotes=["2025-01-01"],
            ),
            grain="day",
            grain_quote="按日",
        ),
        dw_database="dw",
    )
    assert result.issues[0].code == "time_grain_mismatch"


async def test_trend_can_sort_by_validated_time_bucket_alias() -> None:
    """趋势可按已验证时间桶的投影别名排序。"""
    result = await validate_query(
        QueryDraft(
            sql=(
                "SELECT DATE_FORMAT(o.created_at, '%Y-%m') AS month, SUM(o.amount) "
                "FROM dw.orders o GROUP BY DATE_FORMAT(o.created_at, '%Y-%m') "
                "ORDER BY month ASC"
            ),
            table_ids=["table-orders"],
            column_ids=["column-created-at", "column-amount"],
        ),
        _context().model_copy(
            update={"bindings": {"日期": "column-created-at", "金额": "column-amount"}}
        ),
        QueryIntent(
            query_type=QueryType.TREND,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["金额"],
            time_column_quote="日期",
            grain="month",
            grain_quote="按月",
            sorts=[SortIntent(quote="日期", direction="asc")],
        ),
        dw_database="dw",
    )
    assert result.validated is not None


async def test_numeric_aggregate_rejects_text_measure() -> None:
    """SUM 与 AVG 不得依赖 MySQL 对文本字段的隐式数值转换。"""
    context = _context()
    orders = context.physical_schema.tables[0]
    text_orders = orders.model_copy(
        update={
            "columns": [
                column.model_copy(update={"data_type": "VARCHAR(64)"})
                if column.id == "column-amount"
                else column
                for column in orders.columns
            ]
        }
    )
    context = context.model_copy(
        update={
            "physical_schema": context.physical_schema.model_copy(
                update={"tables": [text_orders, *context.physical_schema.tables[1:]]}
            ),
            "bindings": {"金额": "column-amount"},
        }
    )

    result = await validate_query(
        _draft("SELECT SUM(o.amount) FROM dw.orders o"),
        context,
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["金额"],
        ),
        dw_database="dw",
    )

    assert result.issues[0].code == "aggregation_type_mismatch"


def test_grain_with_time_ambiguity_can_continue_clarification() -> None:
    """已回答粒度但仍缺时间字段时可继续澄清。"""
    QueryIntent(
        query_type=QueryType.TREND,
        aggregation="sum",
        aggregation_quote="合计",
        measure_quotes=["销售额"],
        grain="month",
        grain_quote="按月",
        ambiguities=[
            QueryAmbiguity(slot="time", quote="趋势", question="请选择时间字段")
        ],
    ).validate_evidence(["查看销售额趋势，按月", "合计"])


@pytest.mark.parametrize("operator_text", ["=", "!=", "<>", " in ", " like "])
def test_supported_symbolic_filter_cannot_be_omitted(operator_text: str) -> None:
    """受支持的符号和英文过滤操作不得从意图中完全省略。"""
    with pytest.raises(ValueError, match="过滤条件"):
        QueryIntent(
            query_type=QueryType.DETAIL,
            query_type_quote="列出",
            measure_quotes=["订单编号"],
        ).validate_evidence([f"列出地区{operator_text}华东的订单编号"])


def test_duplicate_filter_clause_cannot_replace_another_clause() -> None:
    """重复首项过滤不能冒充原文中的另一项过滤。"""
    duplicate = FilterIntent(
        column_quote="地区",
        operator="eq",
        operator_quote="是",
        value_quotes=["华东"],
        clause_quote="地区是华东",
    )
    with pytest.raises(ValueError, match="一一对应"):
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="count",
            aggregation_quote="数量",
            measure_quotes=["订单"],
            filters=[duplicate, duplicate.model_copy()],
        ).validate_evidence(["地区是华东且状态是完成的订单数量"])


async def test_aggregate_alias_cannot_claim_another_business_identity() -> None:
    """聚合公开别名不能冒充另一业务指标或分组列。"""
    result = await validate_query(
        _draft("SELECT SUM(o.amount) AS profit FROM dw.orders AS o"),
        _context().model_copy(update={"bindings": {"金额": "column-amount"}}),
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="sum",
            aggregation_quote="合计",
            measure_quotes=["金额"],
        ),
        dw_database="dw",
    )
    assert result.validated is None
    assert result.issues[0].code == "projection_alias_mismatch"
