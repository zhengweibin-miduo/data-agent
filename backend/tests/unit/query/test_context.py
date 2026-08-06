"""当前 DDL 权威 Meta 上下文构建测试。"""

from ddl_metadata.meta_projection.models import (
    MetadataCandidate,
    MetadataObjectKind,
    MetadataValueCandidate,
    MetadataValueSearchResult,
)
from models.physical import PhysicalColumn, PhysicalSchema, PhysicalTable
from query.adapters.metadata import QueryMetadataAdapter
from query.application.contracts import QueryClarification
from query.domain import QueryIntent, QueryType


class _Search:
    """记录一次语义召回和后置字段值召回。"""

    def __init__(self, candidates: list[MetadataCandidate]) -> None:
        self.candidates = candidates
        self.calls: list[str] = []
        self.column_ids: set[str] = set()

    async def search_metadata(
        self,
        query: str,
        *,
        table_ids: set[str],
        column_ids: set[str],
    ) -> list[MetadataCandidate]:
        """返回预置的权威候选。"""
        del query
        assert table_ids == {"table-orders"}
        assert column_ids == {"column-amount", "column-region"}
        self.calls.append("metadata")
        return self.candidates

    async def search_values(
        self,
        query: str,
        column_ids: set[str],
    ) -> MetadataValueSearchResult:
        """记录字段范围并返回不完整值提示。"""
        del query
        self.calls.append("values")
        self.column_ids = column_ids
        return MetadataValueSearchResult(
            values=[
                MetadataValueCandidate(
                    column_id="column-region",
                    table_id="table-orders",
                    value="华东",
                    frequency=10,
                )
            ],
            complete=False,
        )


def _schema() -> PhysicalSchema:
    """构造当前来源的表字段 allowlist。"""
    return PhysicalSchema(
        source="erp",
        canonical_ddl="CREATE TABLE orders (amount DECIMAL(10,2), region VARCHAR(20))",
        ddl_hash="ddl",
        schema_fingerprint="schema",
        tables=[
            PhysicalTable(
                id="table-orders",
                name="orders",
                qualified_name="orders",
                columns=[
                    PhysicalColumn(
                        id="column-amount", name="amount", data_type="DECIMAL(10,2)"
                    ),
                    PhysicalColumn(
                        id="column-region", name="region", data_type="VARCHAR(20)"
                    ),
                ],
            )
        ],
    )


def _candidate(
    kind: MetadataObjectKind,
    object_id: str,
    name: str,
    *,
    related: list[str] | None = None,
) -> MetadataCandidate:
    """构造一个已经由 Meta 回读确认的候选。"""
    return MetadataCandidate(
        kind=kind,
        object_id=object_id,
        table_id="table-orders",
        name=name,
        description=name,
        related_column_ids=related or [],
        score=1,
        matched_text=name,
    )


async def test_context_does_not_execute_natural_language_metric_definition() -> None:
    """没有结构化公式的指标即使仅关联一列也必须继续澄清。"""
    search = _Search(
        [
            _candidate(
                MetadataObjectKind.METRIC,
                "metric-sales",
                "销售额",
                related=["column-amount"],
            ),
            _candidate(MetadataObjectKind.COLUMN, "column-region", "地区"),
            _candidate(MetadataObjectKind.COLUMN, "foreign-column", "地区"),
        ]
    )
    adapter = QueryMetadataAdapter(search)

    result = await adapter.build_context(
        "按地区查询销售额",
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            measure_quotes=["销售额"],
            dimension_quotes=["地区"],
        ),
        _schema(),
    )

    assert isinstance(result, QueryClarification)
    assert result.slot == "measure"
    assert search.calls == ["metadata"]


async def test_context_returns_only_highest_impact_clarification() -> None:
    """多个指标候选先于后续槽位只产生一个澄清。"""
    search = _Search(
        [
            _candidate(MetadataObjectKind.METRIC, "metric-paid", "销售额支付口径"),
            _candidate(MetadataObjectKind.METRIC, "metric-order", "销售额下单口径"),
        ]
    )

    result = await QueryMetadataAdapter(search).build_context(
        "查询销售额",
        QueryIntent(query_type=QueryType.AGGREGATE, measure_quotes=["销售额"]),
        _schema(),
    )

    assert isinstance(result, QueryClarification)
    assert result.slot == "measure"
    assert search.calls == ["metadata"]


async def test_formula_metric_requires_physical_column_clarification() -> None:
    """多相关字段的自然语言指标不能被当作可执行公式。"""
    search = _Search(
        [
            _candidate(
                MetadataObjectKind.METRIC,
                "metric-sales",
                "销售额",
                related=["column-amount", "column-region"],
            )
        ]
    )

    result = await QueryMetadataAdapter(search).build_context(
        "销售额",
        QueryIntent(query_type=QueryType.AGGREGATE, measure_quotes=["销售额"]),
        _schema(),
    )

    assert isinstance(result, QueryClarification)
    assert result.slot == "measure"


async def test_time_range_requires_an_explicit_time_column() -> None:
    """时间范围不能被误当字段绑定，缺少时间字段时必须澄清。"""
    search = _Search([])

    result = await QueryMetadataAdapter(search).build_context(
        "查询最近7天销售额",
        QueryIntent(
            query_type=QueryType.AGGREGATE,
            measure_quotes=["销售额"],
            time_quote="最近7天",
        ),
        _schema(),
    )

    assert isinstance(result, QueryClarification)
    assert result.slot == "time"
    assert "时间字段" in result.question
    assert search.calls == ["metadata"]


async def test_empty_semantic_intent_fails_closed_before_planning() -> None:
    """没有任何可绑定语义槽位时不能把完整物理模式交给 planner。"""
    search = _Search([])

    result = await QueryMetadataAdapter(search).build_context(
        "查一下",
        QueryIntent(query_type=QueryType.DETAIL),
        _schema(),
    )

    assert isinstance(result, QueryClarification)
    assert result.slot == "measure"
    assert search.calls == ["metadata"]
