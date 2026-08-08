"""自然语言查询的纯领域契约与 SQL 安全规则。"""

from __future__ import annotations

import asyncio
import re
from dataclasses import InitVar, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Literal

import sqlglot
from pydantic import Field
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope

from models.base import ContractModel
from models.physical import PhysicalSchema


class QueryType(StrEnum):
    """用户请求的业务查询形态。"""

    DETAIL = "detail"
    AGGREGATE = "aggregate"
    RANKING = "ranking"
    TREND = "trend"
    COMPARISON = "comparison"


class QueryMetadataKind(StrEnum):
    """Query 上下文可消费的技术中立元数据类型。"""

    TABLE = "table"
    COLUMN = "column"
    METRIC = "metric"


class QueryMetadataCandidate(ContractModel):
    """由 Meta 适配器转换后的权威查询候选。"""

    kind: QueryMetadataKind = Field(description="查询候选类型。")
    object_id: str = Field(description="权威对象标识。")
    table_id: str | None = Field(default=None, description="所属表标识。")
    name: str = Field(description="权威对象名称。")
    description: str = Field(description="权威对象描述。")
    related_column_ids: list[str] = Field(
        default_factory=list, description="指标关联字段标识。"
    )
    matched_text: str = Field(description="产生召回的有界文本。")


class QueryMetadataValue(ContractModel):
    """由 Meta 适配器转换后的字段值提示。"""

    column_id: str = Field(description="字段标识。")
    table_id: str = Field(description="所属表标识。")
    value: str = Field(description="匹配值。")
    frequency: int = Field(ge=1, description="值频次。")


class FilterIntent(ContractModel):
    """完全由用户原文表达的一项过滤条件。"""

    column_quote: str = Field(
        min_length=1, max_length=256, description="过滤字段原文。"
    )
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "in", "contains"] = Field(
        description="用户明确表达的过滤操作。"
    )
    operator_quote: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="过滤操作符的用户原文证据。",
    )
    value_quotes: list[str] = Field(
        min_length=1,
        max_length=100,
        description="过滤值的用户原文列表。",
    )
    clause_quote: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description="同时包含字段、操作符和值的一项完整用户原文子句。",
    )


class SortIntent(ContractModel):
    """完全由用户原文表达的一项排序。"""

    quote: str = Field(min_length=1, max_length=256, description="排序对象原文。")
    direction: Literal["asc", "desc"] = Field(description="用户明确表达的排序方向。")
    direction_quote: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        description="排序方向的用户原文证据。",
    )


class QueryAmbiguity(ContractModel):
    """模型发现但不能自行补全的一项语义歧义。"""

    slot: Literal["measure", "time", "dimension", "filter", "sort"] = Field(
        description="存在歧义的意图槽位。"
    )
    quote: str = Field(min_length=1, max_length=256, description="歧义对应的用户原文。")
    question: str = Field(min_length=1, max_length=1024, description="建议的澄清问题。")


class QueryIntent(ContractModel):
    """仅由用户原文证据组成的结构化查询意图。"""

    query_type: QueryType = Field(description="用户请求的查询形态。")
    query_type_quote: str | None = Field(
        default=None, max_length=256, description="查询结果形态的用户原文证据。"
    )
    aggregation: Literal["count", "sum", "avg", "min", "max"] | None = Field(
        default=None, description="用户明确表达的聚合运算。"
    )
    aggregation_quote: str | None = Field(
        default=None, max_length=256, description="聚合运算的用户原文证据。"
    )
    measure_quotes: list[str] = Field(
        default_factory=list, max_length=20, description="指标或度量原文。"
    )
    dimension_quotes: list[str] = Field(
        default_factory=list, max_length=20, description="维度原文。"
    )
    filters: list[FilterIntent] = Field(
        default_factory=list, max_length=20, description="过滤条件。"
    )
    time_quote: str | None = Field(
        default=None, max_length=256, description="时间范围原文。"
    )
    time_column_quote: str | None = Field(
        default=None,
        max_length=256,
        description="用户明确指定的时间字段原文。",
    )
    time_filter: FilterIntent | None = Field(
        default=None,
        description="可确定性映射到绑定参数的时间范围契约。",
    )
    grain: Literal["day", "week", "month", "quarter", "year"] | None = Field(
        default=None,
        description="用户明确表达的时间粒度。",
    )
    grain_quote: str | None = Field(
        default=None, max_length=64, description="时间粒度的用户原文证据。"
    )
    sorts: list[SortIntent] = Field(
        default_factory=list, max_length=10, description="排序条件。"
    )
    limit: int | None = Field(
        default=None, gt=0, le=100_000, description="用户明确表达的 Top-N 数量。"
    )
    limit_quote: str | None = Field(
        default=None, max_length=256, description="包含 Top-N 数量的用户原文。"
    )
    ambiguities: list[QueryAmbiguity] = Field(
        default_factory=list, max_length=20, description="未解决歧义。"
    )

    def validate_evidence(self, user_messages: list[str]) -> None:
        """确认每个关键短语都逐字来自同租户用户消息。"""
        quotes = [
            *([self.query_type_quote] if self.query_type_quote else []),
            *self.measure_quotes,
            *([self.aggregation_quote] if self.aggregation_quote else []),
            *self.dimension_quotes,
            *([self.time_quote] if self.time_quote else []),
            *([self.time_column_quote] if self.time_column_quote else []),
            *([self.time_filter.column_quote] if self.time_filter else []),
            *(
                quote
                for quote in (self.time_filter.value_quotes if self.time_filter else [])
            ),
            *(item.column_quote for item in self.filters),
            *(item.operator_quote for item in self.filters if item.operator_quote),
            *(item.clause_quote for item in self.filters if item.clause_quote),
            *(quote for item in self.filters for quote in item.value_quotes),
            *(
                [self.time_filter.operator_quote]
                if self.time_filter and self.time_filter.operator_quote
                else []
            ),
            *(
                [self.time_filter.clause_quote]
                if self.time_filter and self.time_filter.clause_quote
                else []
            ),
            *(item.quote for item in self.sorts),
            *(item.direction_quote for item in self.sorts if item.direction_quote),
            *(item.quote for item in self.ambiguities),
            *([self.limit_quote] if self.limit_quote else []),
            *([self.grain_quote] if self.grain_quote else []),
        ]
        if any(
            not quote.strip() or not any(quote in message for message in user_messages)
            for quote in quotes
        ):
            raise ValueError("QueryIntent 关键短语必须逐字来自用户原文")
        operator_markers = {
            "eq": ("等于", "为", "是", "="),
            "ne": ("不等于", "不是", "!=", "<>"),
            "gt": ("大于", "超过", ">"),
            "gte": ("大于等于", "至少", "不小于", ">="),
            "lt": ("小于", "低于", "少于", "<"),
            "lte": ("小于等于", "至多", "不超过", "<="),
            "in": ("属于", "在", "之一", "in"),
            "contains": ("包含", "含有", "like"),
        }
        unsupported_negations = ("不包含", "不含", "不在", "not in", "not like")
        user_text = " ".join(user_messages).casefold()
        if any(marker in user_text for marker in unsupported_negations):
            raise ValueError("过滤操作包含尚未建模的否定语义")
        normalized_inequalities = {
            "大于等于": "gte",
            "大于或等于": "gte",
            "小于等于": "lte",
            "小于或等于": "lte",
            "不低于": "gte",
            "不少于": "gte",
            "不小于": "gte",
            "不大于": "lte",
            "不高于": "lte",
            "不超过": "lte",
        }
        normalized_equalities = {"不等于": "ne", "不是": "ne"}
        for item in [*self.filters, *([self.time_filter] if self.time_filter else [])]:
            quote = item.operator_quote
            if quote is not None and any(
                marker in quote.casefold() for marker in unsupported_negations
            ):
                raise ValueError("过滤操作包含尚未建模的否定语义")
            evidenced_operator = next(
                (
                    operator
                    for marker, operator in normalized_inequalities.items()
                    if quote is not None and marker in quote.casefold()
                ),
                None,
            )
            evidenced_operator = next(
                (
                    operator
                    for marker, operator in normalized_equalities.items()
                    if quote is not None and marker in quote.casefold()
                ),
                evidenced_operator,
            )
            marker_matches = sorted(
                (
                    (len(marker), operator)
                    for operator, markers in operator_markers.items()
                    for marker in markers
                    if quote is not None and marker in quote.casefold()
                ),
                reverse=True,
            )
            marker_operators = {operator for _, operator in marker_matches}
            if evidenced_operator is None and len(marker_operators) > 1:
                raise ValueError("过滤操作原文不能同时映射到多个操作符")
            evidenced_operator = evidenced_operator or (
                marker_matches[0][1] if marker_matches else None
            )
            if evidenced_operator != item.operator:
                raise ValueError("过滤操作必须携带与枚举精确一致的用户原文证据")
        all_filters = [*self.filters, *([self.time_filter] if self.time_filter else [])]
        unclaimed_different_text = user_text
        for quote in self.dimension_quotes:
            unclaimed_different_text = unclaimed_different_text.replace(
                f"不同{quote}", ""
            )
        if "不同" in unclaimed_different_text or any(
            marker in user_text for marker in ("去重", "唯一", "distinct")
        ):
            raise ValueError("去重语义尚未建模，必须先澄清")
        boolean_text = re.sub(r"(?:大于|小于)\s*或\s*等于", "", user_text)
        if any(marker in boolean_text for marker in ("或", "或者", " or ")):
            raise ValueError("过滤条件包含尚未建模的 OR 关系")
        if len(all_filters) > 1 and any(
            item.clause_quote
            and any(
                marker
                in re.sub(
                    r"(?:大于|小于)\s*或\s*等于", "", item.clause_quote.casefold()
                )
                for marker in ("或", "或者", " or ")
            )
            for item in all_filters
        ):
            raise ValueError("过滤条件包含尚未建模的 OR 关系")
        if len(all_filters) > 1:
            for item in all_filters:
                if item.clause_quote is None or not all(
                    quote in item.clause_quote
                    for quote in (
                        item.column_quote,
                        item.operator_quote or "",
                        *item.value_quotes,
                    )
                ):
                    raise ValueError("多项过滤的字段、操作符和值必须来自同一用户子句")
        direction_markers = {
            "asc": ("升序", "从低到高", "最低", "最小", "asc"),
            "desc": ("降序", "从高到低", "最高", "最大", "desc"),
        }
        for item in self.sorts:
            matches = {
                direction
                for direction, markers in direction_markers.items()
                if item.direction_quote is not None
                and any(marker in item.direction_quote.casefold() for marker in markers)
            }
            if matches != {item.direction}:
                raise ValueError("排序方向必须携带与枚举一致的用户原文证据")
        limit_numbers = (
            [int(value) for value in re.findall(r"(?<!\d)\d+(?!\d)", self.limit_quote)]
            if self.limit_quote
            else []
        )
        if (self.limit is None) != (self.limit_quote is None) or (
            self.limit is not None
            and (len(limit_numbers) != 1 or limit_numbers[0] != self.limit)
        ):
            raise ValueError("Top-N 数量必须有包含同值的用户原文证据")
        if self.limit_quote is not None and not re.search(
            r"(?:前\s*\d+|top\s*\d+|\d+\s*(?:笔|条|个|名|行))",
            self.limit_quote.casefold(),
        ):
            raise ValueError("Top-N 数量必须包含明确的结果截断语义")
        if self.limit is not None and self.query_type != QueryType.RANKING:
            raise ValueError("只有可信排名意图可以限制结果数量")
        if (self.aggregation is None) != (self.aggregation_quote is None):
            raise ValueError("聚合运算必须携带用户原文证据")
        if self.aggregation is not None and len(self.measure_quotes) > 1:
            raise ValueError("多度量必须逐项提供聚合运算契约")
        if self.aggregation_quote is not None:
            markers = {
                "count": ("数量", "个数", "count"),
                "sum": ("总和", "合计", "sum"),
                "avg": ("平均", "均值", "avg"),
                "min": ("最小", "最低", "min"),
                "max": ("最大", "最高", "max"),
            }
            matched = {
                operation
                for operation, words in markers.items()
                if any(word in self.aggregation_quote.casefold() for word in words)
            }
            if matched != {self.aggregation}:
                raise ValueError("聚合运算必须与用户原文精确一致")
        has_time_ambiguity = any(item.slot == "time" for item in self.ambiguities)
        has_measure_ambiguity = any(item.slot == "measure" for item in self.ambiguities)
        if self.time_quote and self.time_filter is None and not has_time_ambiguity:
            raise ValueError("时间范围必须提供可验证的过滤契约")
        if self.grain and self.time_column_quote is None and not has_time_ambiguity:
            raise ValueError("时间粒度必须提供时间字段契约")
        grain_markers = {
            "day": ("日", "天", "day"),
            "week": ("周", "星期", "week"),
            "month": ("月", "month"),
            "quarter": ("季度", "季", "quarter"),
            "year": ("年", "year"),
        }
        grain_matches = {
            grain
            for grain, markers in grain_markers.items()
            if self.grain_quote is not None
            and any(marker in self.grain_quote.casefold() for marker in markers)
        }
        if self.grain and grain_matches != {self.grain}:
            raise ValueError("时间粒度必须携带与枚举一致的用户原文证据")
        shape_markers = {
            QueryType.DETAIL: ("列出", "每笔", "明细", "记录"),
            QueryType.AGGREGATE: ("总和", "合计", "平均", "数量", "最大值", "最小值"),
            QueryType.RANKING: ("前", "top", "最高", "最低", "排名"),
            QueryType.TREND: ("趋势", "按日", "按周", "按月", "季度", "按年"),
            QueryType.COMPARISON: ("比较", "对比", "按"),
        }
        if self.query_type_quote is not None and not any(
            marker in self.query_type_quote.casefold()
            for marker in shape_markers[self.query_type]
        ):
            raise ValueError("查询形态必须与用户原文动作证据一致")
        result_object_quotes = [*self.measure_quotes, *self.dimension_quotes]
        if self.query_type_quote is None and user_text.strip() in {
            quote.casefold() for quote in result_object_quotes
        }:
            raise ValueError("查询形态必须具有明确的用户原文动作证据")
        trend_markers = (
            "趋势",
            "按日",
            "按周",
            "按月",
            "按季",
            "按年",
            "每日",
            "每周",
            "每月",
            "每季",
            "每季度",
            "每年",
        )
        if any(marker in user_text for marker in trend_markers) and (
            self.query_type != QueryType.TREND
            or (self.grain is None and not has_time_ambiguity)
            or (self.aggregation is None and not has_measure_ambiguity)
        ):
            raise ValueError("用户明确表达的趋势形态必须完整映射到查询意图")
        grouping_matches = re.findall(
            r"按(?!照|日|周|月|季|季度|年)(.+?)(?=统计|查询|查看|展示|比较|对比|。|；|;|$)",
            user_text,
        )
        explicit_dimensions = [
            dimension
            for match in grouping_matches
            if not re.search(
                r"(?:升序|降序|从低到高|从高到低|\basc\b|\bdesc\b|列出)",
                match,
                re.IGNORECASE,
            )
            for dimension in re.split(r"(?:和|与|及|、|，|,)", match)
            if dimension.strip()
        ]
        explicit_dimensions = [item.strip(" 的") for item in explicit_dimensions]
        if explicit_dimensions and any(
            not any(
                dimension in quote or quote in dimension
                for quote in self.dimension_quotes
            )
            for dimension in explicit_dimensions
        ):
            raise ValueError("用户明确表达的分组维度必须完整映射到查询意图")
        has_sort_ambiguity = any(item.slot == "sort" for item in self.ambiguities)
        if re.search(
            r"(?:前\s*\d+|top\s*\d+|\d+\s*(?:条|笔|个|名|行|项))",
            user_text,
        ) and (
            self.query_type != QueryType.RANKING
            or self.limit is None
            or (not self.sorts and not has_sort_ambiguity)
        ):
            raise ValueError("用户明确表达的 Top-N 形态必须完整映射到查询意图")
        explicit_time_range = re.search(
            r"(?:\d{4}\s*年(?:\d{1,2}\s*月(?:\d{1,2}\s*日)?)?|"
            r"\d{4}-\d{1,2}-\d{1,2}|今年|去年|本月|上月|"
            r"最近\s*\d+\s*(?:天|周|月|年))",
            user_text,
        )
        if explicit_time_range and not (
            self.time_quote
            and ((self.time_column_quote and self.time_filter) or has_time_ambiguity)
        ):
            raise ValueError("用户明确表达的时间范围必须完整映射到查询意图")
        explicit_operator_pattern = re.compile(
            r"(?:!=|<>|>=|<=|(?<![<>!])=(?!=)|\b(?:in|like)\b)", re.IGNORECASE
        )
        if (
            any(
                marker in user_text
                for marker in (
                    "大于",
                    "小于",
                    "等于",
                    "超过",
                    "低于",
                    "至少",
                    "至多",
                    "包含",
                )
            )
            or re.search(r"\S+(?:是|为|属于)(?!(?:多少|什么|否))\S+", user_text)
            or explicit_operator_pattern.search(user_text)
        ) and not all_filters:
            raise ValueError("用户明确表达的过滤条件必须完整映射到查询意图")
        filter_evidence_text = re.sub(r"(?:是|为)(?:多少|什么)|是否", "", user_text)
        filter_evidence_text = re.sub(r"(?:以及|和)", "且", filter_evidence_text)
        filter_operator_pattern = re.compile(
            r"(?:大于或等于|小于或等于|大于等于|小于等于|"
            r"不低于|不少于|不大于|不超过|大于|小于|超过|低于|至少|至多|"
            r"等于|属于|包含|是|为|!=|<>|>=|<=|(?<![<>!])=(?!=)|"
            r"\b(?:in|like)\b)",
            re.IGNORECASE,
        )
        explicit_filter_clauses = [
            clause.strip()
            for clause in re.split(r"[且，。,.；;]", filter_evidence_text)
            if filter_operator_pattern.search(clause)
        ]
        if len(explicit_filter_clauses) > len(all_filters):
            raise ValueError("用户明确表达的每项过滤条件必须完整映射到查询意图")
        if len(all_filters) > 1:
            clause_quotes = [item.clause_quote for item in all_filters]
            clause_matches = [
                [
                    clause
                    for clause in explicit_filter_clauses
                    if quote is not None and quote in clause
                ]
                for quote in clause_quotes
            ]
            if (
                None in clause_quotes
                or len(set(clause_quotes)) != len(clause_quotes)
                or any(len(matches) != 1 for matches in clause_matches)
                or len({matches[0] for matches in clause_matches if matches})
                != len(explicit_filter_clauses)
            ):
                raise ValueError("每项过滤条件必须与不同的用户原文子句一一对应")
        aggregation_action = any(
            marker in user_text for marker in ("总和", "合计", "平均", "均值")
        )
        aggregation_action = aggregation_action or any(
            marker in user_text
            and not any(marker in quote for quote in self.measure_quotes)
            for marker in ("数量", "个数")
        )
        aggregation_action = aggregation_action or (
            self.query_type != QueryType.RANKING
            and any(
                marker in user_text
                for marker in ("最大值", "最大", "最高", "最小值", "最小", "最低")
            )
        )
        if aggregation_action and self.aggregation is None:
            raise ValueError("用户明确表达的聚合必须完整映射到查询意图")
        if (
            any(
                marker in user_text
                for marker in ("升序", "降序", "从低到高", "从高到低")
            )
            or re.search(r"\b(?:asc|desc)\b", user_text, re.IGNORECASE)
        ) and not self.sorts:
            raise ValueError("用户明确表达的排序必须完整映射到查询意图")
        explicit_sorts = re.findall(
            r"([^，。,.；;、]+?)(升序|降序|从低到高|从高到低|\basc\b|\bdesc\b)",
            user_text,
            re.IGNORECASE,
        )
        if len(explicit_sorts) > len(self.sorts):
            raise ValueError("用户明确表达的每项排序必须完整映射到查询意图")
        if self.query_type == QueryType.DETAIL:
            detail_evidence_text = user_text
            for item in all_filters:
                if item.clause_quote:
                    detail_evidence_text = detail_evidence_text.replace(
                        item.clause_quote, ""
                    )
            detail_evidence_text = re.sub(
                r"((?:列出|展示|查看))(?:且|和|以及|，|,|的)*",
                r"\1",
                detail_evidence_text,
            )
            detail_match = re.search(
                r"(?:列出|展示|查看)(.+?)(?=(?:大于|小于|等于|属于|包含|"
                r"升序|降序|前\s*\d+|top\s*\d+|的记录|的订单|$))",
                detail_evidence_text.rstrip(" ，,。"),
            )
            if detail_match:
                explicit_results = [
                    item.strip(" 的")
                    for item in re.split(r"(?:和|与|及|、|，|,)", detail_match.group(1))
                    if item.strip(" 的")
                ]
                result_quotes = [*self.measure_quotes, *self.dimension_quotes]
                if any(
                    not any(item in quote or quote in item for quote in result_quotes)
                    for item in explicit_results
                ):
                    raise ValueError("用户明确表达的每个明细结果字段必须进入查询意图")
        if (
            self.dimension_quotes
            and not explicit_dimensions
            and not (
                self.query_type_quote
                and any(
                    marker in self.query_type_quote.casefold()
                    for marker in ("按", "比较", "对比", "趋势", "排名")
                )
            )
        ):
            raise ValueError("分组查询必须携带明确的用户动作证据")
        shape_is_consistent = {
            QueryType.DETAIL: self.aggregation is None and self.grain is None,
            QueryType.AGGREGATE: (
                self.aggregation is not None
                and not self.dimension_quotes
                and not self.sorts
                and self.limit is None
                and self.grain is None
            ),
            QueryType.RANKING: self.limit is not None
            and (bool(self.sorts) or has_sort_ambiguity),
            QueryType.TREND: (
                (self.aggregation is not None or has_measure_ambiguity)
                and (self.grain is not None or has_time_ambiguity)
            ),
            QueryType.COMPARISON: (
                self.aggregation is not None and bool(self.dimension_quotes)
            ),
        }[self.query_type]
        if not shape_is_consistent:
            raise ValueError("查询形态必须与用户证据槽位确定性一致")


QueryParameter = str | int | float | Decimal | bool | None


class QueryDraft(ContractModel):
    """模型生成但尚不可执行的 SQL 草稿。"""

    sql: str = Field(
        min_length=1, max_length=65535, description="单条 MySQL 查询 SQL。"
    )
    params: dict[str, QueryParameter] = Field(
        default_factory=dict, description="命名绑定参数。"
    )
    table_ids: list[str] = Field(
        min_length=1, max_length=50, description="声明引用的表标识。"
    )
    column_ids: list[str] = Field(
        default_factory=list, max_length=500, description="声明引用的字段标识。"
    )
    metric_ids: list[str] = Field(
        default_factory=list, max_length=50, description="声明使用的指标标识。"
    )


class QueryContext(ContractModel):
    """当前 DDL 作用域内的权威、可裁剪查询上下文。"""

    physical_schema: PhysicalSchema = Field(description="确定性解析的当前物理模式。")
    candidates: list[QueryMetadataCandidate] = Field(
        default_factory=list, description="已绑定 Meta 候选。"
    )
    values: list[QueryMetadataValue] = Field(
        default_factory=list, description="字段值提示候选。"
    )
    value_search_complete: bool = Field(
        default=False, description="值候选投影是否完整。"
    )
    bindings: dict[str, str] = Field(
        default_factory=dict, description="用户原文到权威对象标识的绑定。"
    )
    relationships_authoritative: bool = Field(
        default=True, description="关系是否已由权威 Meta 快照核验。"
    )


class SQLValidationIssue(ContractModel):
    """可安全反馈给一次修复模型的稳定 SQL 问题。"""

    code: str = Field(description="稳定问题代码。")
    object_name: str | None = Field(
        default=None, max_length=128, description="相关对象名称。"
    )


_VALIDATED_TOKEN = object()


@dataclass(frozen=True, slots=True)
class ValidatedQuery:
    """只能由本模块静态门禁创建的可执行只读查询。"""

    sql: str
    params: dict[str, QueryParameter]
    target_tables: tuple[str, ...]
    table_ids: tuple[str, ...]
    column_ids: tuple[str, ...]
    metric_ids: tuple[str, ...]
    _token: InitVar[object]

    def __post_init__(self, _token: object) -> None:
        """阻止调用方绕过静态门禁直接构造。"""
        if _token is not _VALIDATED_TOKEN:
            raise TypeError("ValidatedQuery 只能由 validate_query 创建")


@dataclass(frozen=True, slots=True)
class QueryValidationResult:
    """静态 SQL 门禁的成功值或稳定问题列表。"""

    validated: ValidatedQuery | None
    issues: tuple[SQLValidationIssue, ...] = ()


def _failed(code: str, object_name: str | None = None) -> QueryValidationResult:
    """构造一个稳定且无原始 SQL 的失败结果。"""
    return QueryValidationResult(
        None, (SQLValidationIssue(code=code, object_name=object_name),)
    )


def _physical_sources(scope: Scope) -> dict[str, exp.Table]:
    """返回当前 SQL scope 直接引用的物理表，不展开 CTE。"""
    return {
        alias: source
        for alias, source in scope.sources.items()
        if isinstance(source, exp.Table)
    }


def _predicate_contract(
    predicate: exp.Expression,
    columns_by_coordinate: dict[tuple[str, str], str],
    params: dict[str, QueryParameter],
) -> tuple[str, str, list[QueryParameter]] | None:
    """把一个简单绑定谓词规范化为字段、操作符和值。"""
    operators: dict[type[exp.Expression], str] = {
        exp.EQ: "eq",
        exp.NEQ: "ne",
        exp.GT: "gt",
        exp.GTE: "gte",
        exp.LT: "lt",
        exp.LTE: "lte",
        exp.In: "in",
        exp.Like: "contains",
    }
    operator = operators.get(type(predicate))
    if operator is None or not isinstance(predicate.this, exp.Column):
        return None
    column = predicate.this
    column_id = columns_by_coordinate.get((column.table, column.name))
    if column_id is None:
        matches = {
            candidate_id
            for (alias, name), candidate_id in columns_by_coordinate.items()
            if name == column.name and (not column.table or alias == column.table)
        }
        if len(matches) != 1:
            return None
        column_id = matches.pop()
    value_nodes = (
        list(predicate.expressions)
        if isinstance(predicate, exp.In)
        else [predicate.expression]
    )
    if not value_nodes or not all(
        isinstance(node, exp.Placeholder) for node in value_nodes
    ):
        return None
    values = [params[str(node.this)] for node in value_nodes]
    return column_id, operator, values


def _values_match(
    operator: str, values: list[QueryParameter], quotes: list[QueryParameter]
) -> bool:
    """按用户证据精确比较谓词绑定值。"""
    normalized = [str(value) for value in values]
    if operator == "contains":
        string_quotes = [str(quote) for quote in quotes]
        if any("%" in quote or "_" in quote for quote in string_quotes):
            return False
        return normalized == [f"%{quote}%" for quote in string_quotes]
    return normalized == [str(quote) for quote in quotes]


def _normalized_filter_values(
    item: FilterIntent, *, data_type: str = ""
) -> list[QueryParameter]:
    """只从逐字证据确定性解析常用数字与本地化日期绑定值。"""
    normalized_type = data_type.upper()
    if not re.match(
        r"^(?:TINYINT|SMALLINT|MEDIUMINT|INT|INTEGER|BIGINT|DECIMAL|NUMERIC|FLOAT|DOUBLE|REAL|DATE|DATETIME|TIMESTAMP)\b",
        normalized_type,
    ):
        return list(item.value_quotes)
    values: list[QueryParameter] = []
    for quote in item.value_quotes:
        compact = quote.replace(",", "")
        if re.fullmatch(r"[-+]?\d+", compact):
            values.append(int(compact))
            continue
        if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", compact):
            try:
                values.append(Decimal(compact))
            except InvalidOperation:
                values.append(quote)
            continue
        date_match = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", quote)
        if date_match:
            year, month, day = map(int, date_match.groups())
            try:
                values.append(date(year, month, day).isoformat())
            except ValueError:
                values.append(quote)
            continue
        values.append(quote)
    return values


def _column_id(
    column: exp.Column,
    columns_by_coordinate: dict[tuple[str, str], str],
) -> str | None:
    """把已通过 allowlist 的字段表达式映射为权威字段标识。"""
    direct = columns_by_coordinate.get((column.table, column.name))
    if direct is not None:
        return direct
    matches = {
        candidate_id
        for (alias, name), candidate_id in columns_by_coordinate.items()
        if name == column.name and (not column.table or alias == column.table)
    }
    return matches.pop() if len(matches) == 1 else None


def _predicate_tree_supported(expression: exp.Expression) -> bool:
    """当前过滤契约只允许由 AND 连接的受支持原子谓词。"""
    if isinstance(expression, exp.Paren):
        return _predicate_tree_supported(expression.this)
    if isinstance(expression, exp.And):
        return _predicate_tree_supported(expression.this) and _predicate_tree_supported(
            expression.expression
        )
    return isinstance(
        expression,
        (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Like),
    )


async def validate_query(
    draft: QueryDraft,
    context: QueryContext,
    intent: QueryIntent,
    *,
    dw_database: str,
) -> QueryValidationResult:
    """在线程中用 SQLGlot 和当前 DDL allowlist 验证一条 MySQL SELECT。"""
    return await asyncio.to_thread(
        _validate_query_sync,
        draft,
        context,
        intent,
        dw_database=dw_database,
    )


def _validate_query_sync(
    draft: QueryDraft,
    context: QueryContext,
    intent: QueryIntent,
    *,
    dw_database: str,
) -> QueryValidationResult:
    """拥有完整 SQLGlot 解析、scope 分析和领域模型构造流水线。"""
    if intent.time_quote and intent.time_filter is None:
        return _failed("time_contract_mismatch")
    if intent.grain and intent.time_column_quote is None:
        return _failed("time_contract_mismatch")
    # 步骤一：拒绝注释、多语句、解析失败和任何非 SELECT 根节点。
    if any(marker in draft.sql for marker in ("--", "/*", "*/", "#")):
        return _failed("comment_forbidden")
    try:
        statements = sqlglot.parse(draft.sql, read="mysql")
    except ParseError:
        return _failed("syntax_invalid")
    if len(statements) != 1:
        return _failed("statement_count")
    root = statements[0]
    if not isinstance(root, exp.Select):
        return _failed("select_only")
    if any(
        isinstance(
            node,
            (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Command),
        )
        for node in root.walk()
    ):
        return _failed("select_only")
    if any(not isinstance(star.parent, exp.Count) for star in root.find_all(exp.Star)):
        return _failed("select_star")
    if root.find(exp.Parameter) is not None:
        return _failed("user_variable_forbidden")
    if root.find(exp.Lock) is not None:
        return _failed("lock_forbidden")
    if root.find(exp.Distinct) is not None:
        return _failed("distinct_forbidden")

    # 派生输出只有可证明的直接字段透传才可复用物理字段身份。聚合、算术、
    # 集合运算等表达式没有结构化血缘契约，禁止按同名字段全局回退。
    for scope in traverse_scope(root):
        for source in scope.sources.values():
            if not isinstance(source, Scope):
                continue
            if not isinstance(source.expression, exp.Select):
                return _failed("derived_lineage_unsupported")
            if (
                source.expression.args.get("group") is not None
                or source.expression.args.get("qualify") is not None
                or source.expression.find(exp.Window) is not None
            ):
                return _failed("derived_lineage_unsupported")
            for projection in source.expression.expressions:
                expression = (
                    projection.this if isinstance(projection, exp.Alias) else projection
                )
                if not isinstance(expression, exp.Column):
                    return _failed("derived_lineage_unsupported")
                # 派生输出名称必须保持源字段名称。没有逐 scope 的显式血缘契约
                # 时，允许改名会令外层同名字段被误映射到另一物理列。
                if projection.alias and projection.alias != expression.name:
                    return _failed("derived_lineage_unsupported")

    # 步骤二：真实物理表必须显式位于 DW schema，并来自当前 DDL allowlist。
    tables_by_name = {table.name: table for table in context.physical_schema.tables}
    physical_tables = [
        table
        for scope in traverse_scope(root)
        for table in _physical_sources(scope).values()
    ]
    # 当前意图没有关系路径契约；同一物理表的重复别名不是可信绑定，且会
    # 通过自连接改变行基数。
    joined_physical_tables = [
        join.this
        for join in root.find_all(exp.Join)
        if isinstance(join.this, exp.Table)
    ]
    if any(
        joined.db == table.db and joined.name == table.name
        for joined in joined_physical_tables
        for table in physical_tables
        if joined is not table
    ):
        return _failed("table_binding_mismatch")
    for table_node in physical_tables:
        if table_node.args.get("partition") is not None:
            return _failed("table_modifier_forbidden", table_node.name)
        if table_node.db != dw_database:
            return _failed("schema_forbidden", table_node.db or table_node.name)
        if table_node.name not in tables_by_name:
            return _failed("table_unknown", table_node.name)

    # 步骤三：函数、文件输出、锁和谓词中的裸值全部失败关闭。
    dangerous_functions = {
        "SLEEP",
        "BENCHMARK",
        "LOAD_FILE",
        "GET_LOCK",
        "RELEASE_LOCK",
        "IS_FREE_LOCK",
        "IS_USED_LOCK",
    }
    for function in root.find_all(exp.Func):
        name = function.sql_name().upper()
        if isinstance(function, exp.Anonymous):
            name = function.name.upper()
        if name in dangerous_functions:
            return _failed("function_forbidden", name)
    if (
        root.find(exp.Into) is not None
        or "OUTFILE" in draft.sql.upper()
        or "DUMPFILE" in draft.sql.upper()
    ):
        return _failed("file_output_forbidden")
    for literal in root.find_all(exp.Literal):
        if literal.find_ancestor(exp.Where, exp.Having, exp.Join) is not None:
            return _failed("literal_forbidden")
    for literal in (*root.find_all(exp.Boolean), *root.find_all(exp.Null)):
        if literal.find_ancestor(exp.Where, exp.Having, exp.Join) is not None:
            return _failed("literal_forbidden")

    # 步骤四：命名 placeholder 与参数键必须完全一致。
    placeholders = {str(item.this) for item in root.find_all(exp.Placeholder)}
    if placeholders != set(draft.params):
        return _failed("parameter_mismatch")

    # 步骤五：用户未表达 Top-N 时禁止固定 LIMIT；分页偏移一律失败关闭。
    nested_limits = [
        select
        for select in root.find_all(exp.Select)
        if select is not root
        and (
            select.args.get("limit") is not None
            or select.args.get("offset") is not None
        )
    ]
    if nested_limits:
        return _failed("nested_limit_forbidden")
    limit = root.args.get("limit")
    if root.args.get("offset") is not None or (
        isinstance(limit, exp.Limit) and limit.args.get("offset") is not None
    ):
        return _failed("offset_forbidden")
    if intent.limit is None and limit is not None:
        return _failed("limit_unexpected")
    if intent.limit is not None:
        expression = limit.expression if isinstance(limit, exp.Limit) else None
        if (
            not isinstance(expression, exp.Literal)
            or not expression.is_int
            or int(expression.this) != intent.limit
        ):
            return _failed("limit_mismatch")

    # 步骤六：按 SQL scope 解析真实物理字段，CTE 外层引用由其内层权威字段覆盖。
    referenced_columns: set[str] = set()
    for scope in traverse_scope(root):
        sources = _physical_sources(scope)
        for column_node in scope.columns:
            if (
                not column_node.table
                and column_node.name in scope.expression.named_selects
                and column_node.find_ancestor(exp.Order, exp.Group, exp.Having)
                is not None
            ):
                continue
            if column_node.table:
                source = scope.sources.get(column_node.table)
                if isinstance(source, Scope):
                    if column_node.name not in source.expression.named_selects:
                        return _failed("column_unknown", column_node.name)
                    continue
                table_node = sources.get(column_node.table)
                candidates = (
                    [tables_by_name[table_node.name]] if table_node is not None else []
                )
            else:
                candidates = [
                    tables_by_name[table_node.name]
                    for table_node in sources.values()
                    if any(
                        column.name == column_node.name
                        for column in tables_by_name[table_node.name].columns
                    )
                ]
                derived_matches = sum(
                    column_node.name in source.expression.named_selects
                    for source in scope.sources.values()
                    if isinstance(source, Scope)
                )
            matching = [
                column
                for table in candidates
                for column in table.columns
                if column.name == column_node.name
            ]
            if len(matching) + (derived_matches if not column_node.table else 0) != 1:
                return _failed("column_unknown", column_node.name)
            if matching:
                referenced_columns.add(matching[0].id)

    # 步骤七：JOIN 的完整 ON 树只能由 AND 连接当前 DDL 的 FK 等式。
    fk_edges: set[frozenset[str]] = set()
    fk_constraints: dict[str, set[frozenset[str]]] = {}
    fk_constraint_tables: dict[str, tuple[str, str]] = {}
    fk_constraint_targets: dict[str, set[str]] = {}
    table_by_qualified = {
        key.casefold(): table
        for table in context.physical_schema.tables
        for key in (table.qualified_name, table.name)
    }
    for relation in context.physical_schema.relationships:
        target_table = table_by_qualified.get(relation.target_table.casefold())
        if target_table is None:
            continue
        target_column = next(
            (
                column
                for column in target_table.columns
                if column.name.casefold() == relation.target_column.casefold()
            ),
            None,
        )
        target_primary_key = {name.casefold() for name in target_table.primary_key}
        if (
            target_column is not None
            and target_column.name.casefold() in target_primary_key
        ):
            edge = frozenset((relation.source_column_id, target_column.id))
            fk_edges.add(edge)
            constraint_id = (
                relation.constraint_id or f"legacy:{relation.source_column_id}"
            )
            fk_constraints.setdefault(constraint_id, set()).add(edge)
            fk_constraint_targets.setdefault(constraint_id, set()).add(
                target_column.name.casefold()
            )
            fk_constraint_tables[constraint_id] = (
                relation.source_table_id,
                target_table.id,
            )
    for constraint_id in list(fk_constraints):
        target_table = next(
            table
            for table in context.physical_schema.tables
            if table.id == fk_constraint_tables[constraint_id][1]
        )
        if fk_constraint_targets[constraint_id] != {
            name.casefold() for name in target_table.primary_key
        }:
            del fk_constraints[constraint_id]
    aliases = {
        table.alias_or_name: tables_by_name[table.name] for table in physical_tables
    }
    columns_by_coordinate = {
        (alias, column.name): column.id
        for alias, table in aliases.items()
        for column in table.columns
    }
    joins = list(root.find_all(exp.Join))
    # 请求 DDL 不是关系授权来源。当前 Meta 端口尚未提供权威关系快照，
    # 因此在建立该契约前对 JOIN 失败关闭。
    if joins and not context.relationships_authoritative:
        return _failed("join_authority_unavailable")
    actual_join_directions: set[tuple[str, str]] = set()
    for join in joins:
        if (
            join.kind.upper() == "CROSS"
            or str(join.side or "").upper() not in ("", "INNER")
            or join.args.get("on") is None
        ):
            return _failed("join_forbidden")
        on = join.args["on"]
        def supported_join_tree(node: exp.Expression) -> bool:
            if isinstance(node, exp.And):
                return supported_join_tree(node.this) and supported_join_tree(
                    node.expression
                )
            return (
                isinstance(node, exp.EQ)
                and isinstance(node.this, exp.Column)
                and isinstance(node.expression, exp.Column)
            )

        if not supported_join_tree(on):
            return _failed("join_unsupported")
        equalities = list(on.find_all(exp.EQ))
        join_pairs = {
            frozenset(
                (
                    columns_by_coordinate.get((left.table, left.name), ""),
                    columns_by_coordinate.get((right.table, right.name), ""),
                )
            )
            for equality in equalities
            if isinstance((left := equality.this), exp.Column)
            and isinstance((right := equality.expression), exp.Column)
        }
        joined_alias = join.this.alias_or_name
        if (
            not join_pairs
            or not all(pair in fk_edges and "" not in pair for pair in join_pairs)
            or not any(
                any(
                    column.table == joined_alias
                    for equality in equalities
                    for column in (equality.this, equality.expression)
                    if isinstance(column, exp.Column)
                    and columns_by_coordinate.get((column.table, column.name), "")
                    in pair
                )
                for pair in join_pairs
            )
        ):
            return _failed("join_unsupported")
        matched_constraints = [
            constraint_id
            for constraint_id, edges in fk_constraints.items()
            if edges == join_pairs
        ]
        if not matched_constraints:
            return _failed("join_unsupported")
        actual_join_directions.update(
            fk_constraint_tables[constraint_id] for constraint_id in matched_constraints
        )

    # 步骤八：模型声明必须与 AST 实际引用以及当前权威指标完全一致。
    referenced_tables = {tables_by_name[table.name].id for table in physical_tables}
    if referenced_tables != set(draft.table_ids):
        return _failed("table_id_mismatch")
    if referenced_columns != set(draft.column_ids):
        return _failed("column_id_mismatch")
    allowed_metric_ids = {
        candidate.object_id
        for candidate in context.candidates
        if candidate.kind == QueryMetadataKind.METRIC
    }
    if not set(draft.metric_ids).issubset(allowed_metric_ids):
        return _failed("metric_id_mismatch")
    required_ids = set(context.bindings.values())
    allowed_table_ids = {table.id for table in context.physical_schema.tables}
    required_metric_ids = required_ids & allowed_metric_ids
    required_table_ids = required_ids & allowed_table_ids
    required_column_ids = required_ids - required_metric_ids - required_table_ids
    for candidate in context.candidates:
        if candidate.object_id in required_metric_ids:
            required_column_ids.update(candidate.related_column_ids)
    if list(root.find_all(exp.Join)):
        result_object_ids = {
            context.bindings[quote]
            for quote in intent.measure_quotes
            if context.bindings.get(quote) is not None
        }
        measure_owner_tables = result_object_ids & allowed_table_ids
        measure_owner_tables.update({
            column_owner
            for column_id in result_object_ids
            if (
                column_owner := next(
                    (
                        table.id
                        for table in context.physical_schema.tables
                        for column in table.columns
                        if column.id == column_id
                    ),
                    None,
                )
            )
            is not None
        })
        parent_tables_by_child: dict[str, set[str]] = {}
        for child_table, parent_table in actual_join_directions:
            parent_tables_by_child.setdefault(child_table, set()).add(parent_table)
        for measure_table in measure_owner_tables:
            reachable = {measure_table}
            pending = [measure_table]
            while pending:
                current = pending.pop()
                for parent in parent_tables_by_child.get(current, set()):
                    if parent not in reachable:
                        reachable.add(parent)
                        pending.append(parent)
            if not referenced_tables.issubset(reachable):
                return _failed("join_cardinality_unsupported")
    if not required_metric_ids.issubset(draft.metric_ids):
        return _failed("binding_missing")
    if not required_table_ids.issubset(referenced_tables):
        return _failed("binding_missing")
    if not required_column_ids.issubset(referenced_columns):
        return _failed("binding_missing")
    column_owner = {
        column.id: table.id
        for table in context.physical_schema.tables
        for column in table.columns
    }
    allowed_bound_tables = required_table_ids | {
        column_owner[column_id]
        for column_id in required_column_ids
        if column_id in column_owner
    }
    if allowed_bound_tables and not referenced_tables.issubset(allowed_bound_tables):
        return _failed("table_binding_mismatch")

    root_scope = next(
        scope for scope in traverse_scope(root) if scope.expression is root
    )
    reachable_expressions: set[int] = set()

    def visit_scope(scope: Scope) -> None:
        if id(scope.expression) in reachable_expressions:
            return
        reachable_expressions.add(id(scope.expression))
        for source in scope.sources.values():
            if isinstance(source, Scope):
                visit_scope(source)

    visit_scope(root_scope)
    reachable_having = [
        clause
        for clause in root.find_all(exp.Having)
        if (owner := clause.find_ancestor(exp.Select)) is not None
        and id(owner) in reachable_expressions
    ]
    # FilterIntent 只描述聚合前物理行过滤；HAVING 需要独立的聚合谓词契约。
    if reachable_having:
        return _failed("predicate_mismatch")
    predicate_clauses = [
        clause
        for clause in root.find_all(exp.Where)
        if (owner := clause.find_ancestor(exp.Select)) is not None
        and id(owner) in reachable_expressions
    ]
    predicate_nodes = [
        node
        for clause in predicate_clauses
        for node in clause.this.walk()
        if isinstance(
            node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Like)
        )
    ]
    predicate_contracts = [
        _predicate_contract(node, columns_by_coordinate, draft.params)
        for node in predicate_nodes
    ]
    if any(not _predicate_tree_supported(clause.this) for clause in predicate_clauses):
        return _failed("predicate_mismatch")
    expected_filters = [*intent.filters]
    if intent.time_filter is not None:
        expected_filters.append(intent.time_filter)
    column_types = {
        column.id: column.data_type.upper()
        for table in context.physical_schema.tables
        for column in table.columns
    }
    numeric_type = re.compile(
        r"^(?:TINYINT|SMALLINT|MEDIUMINT|INT|INTEGER|BIGINT|DECIMAL|NUMERIC|FLOAT|DOUBLE|REAL)\b"
    )
    temporal_type = re.compile(r"^(?:DATE|DATETIME|TIMESTAMP)\b")
    expected_contracts = [
        (
            context.bindings.get(item.column_quote),
            item.operator,
            _normalized_filter_values(
                item,
                data_type=column_types.get(
                    context.bindings.get(item.column_quote) or "", ""
                ),
            ),
        )
        for item in expected_filters
    ]
    for item in [
        *intent.filters,
        *([intent.time_filter] if intent.time_filter else []),
    ]:
        column_id = context.bindings.get(item.column_quote)
        data_type = column_types.get(column_id or "", "")
        values = _normalized_filter_values(item, data_type=data_type)
        if numeric_type.match(data_type) and any(
            not isinstance(value, (int, Decimal)) for value in values
        ):
            return _failed("filter_value_type_mismatch")
        if temporal_type.match(data_type) and any(
            not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
            for value in values
        ):
            return _failed("filter_value_type_mismatch")
        if (
            re.match(r"^(?:DATETIME|TIMESTAMP)\b", data_type)
            and item.operator in {"eq", "ne"}
            and all(
                isinstance(value, str)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
                for value in values
            )
        ):
            return _failed("filter_value_type_mismatch")

    def comparable_actual(
        item: tuple[str, str, list[QueryParameter]] | None,
    ) -> tuple[str, str, tuple[str, ...]] | None:
        if item is None:
            return None
        return item[0], item[1], tuple(str(value) for value in item[2])

    comparable_expected = [
        (column_id, operator, tuple(values))
        for column_id, operator, values in expected_contracts
        if column_id is not None
    ]
    actual_multiset = sorted(
        (
            item
            for item in (comparable_actual(value) for value in predicate_contracts)
            if item
        ),
        key=repr,
    )
    expected_multiset = sorted(comparable_expected, key=repr)
    if (
        len(comparable_expected) != len(expected_contracts)
        or len(actual_multiset) != len(predicate_contracts)
        or len(actual_multiset) != len(expected_multiset)
        or any(
            actual[:2] != expected[:2]
            or not _values_match(actual[1], list(actual[2]), list(expected[2]))
            for actual, expected in zip(actual_multiset, expected_multiset, strict=True)
        )
    ):
        return _failed("predicate_mismatch")

    # 步骤九：查询形态、时间粒度和排序必须与可信 QueryIntent 精确一致。
    top_level_aggregates = [
        aggregate
        for projection in root.expressions
        for aggregate in projection.find_all(exp.AggFunc)
        if aggregate.find_ancestor(exp.Select) is root
    ]
    has_aggregate = bool(top_level_aggregates)
    has_group = root.args.get("group") is not None
    group = root.args.get("group")
    if isinstance(group, exp.Group) and any(
        value for key, value in group.args.items() if key != "expressions"
    ):
        return _failed("group_modifier_forbidden")
    shape_valid = {
        QueryType.DETAIL: not has_aggregate and not has_group,
        QueryType.AGGREGATE: has_aggregate and not has_group,
        QueryType.RANKING: limit is not None
        and (
            (
                bool(
                    dimension_ids := {
                        context.bindings[quote]
                        for quote in intent.dimension_quotes
                        if context.bindings.get(quote) is not None
                    }
                )
                and has_aggregate
                and has_group
            )
            or (not dimension_ids and not has_aggregate and not has_group)
        ),
        QueryType.TREND: has_aggregate and has_group,
        QueryType.COMPARISON: has_aggregate and has_group,
    }[intent.query_type]
    if not shape_valid:
        return _failed("query_shape_mismatch")
    if intent.query_type == QueryType.AGGREGATE and (
        len(root.expressions) != 1
        or not isinstance(
            root.expressions[0].this
            if isinstance(root.expressions[0], exp.Alias)
            else root.expressions[0],
            exp.AggFunc,
        )
    ):
        return _failed("projection_mismatch")

    # 聚合结果的公开业务标签尚无可信意图契约；只允许无业务
    # 身份的稳定通用标签，并拒绝与其他结果列重名。
    public_labels = [
        projection.alias_or_name.casefold()
        for projection in root.expressions
        if projection.alias_or_name
    ]
    if len(public_labels) != len(set(public_labels)):
        return _failed("projection_alias_mismatch")
    for projection in root.expressions:
        if isinstance(projection, exp.Alias) and isinstance(
            projection.this, exp.AggFunc
        ) and projection.alias.casefold() != "total":
            return _failed("projection_alias_mismatch")

    if intent.query_type == QueryType.DETAIL:
        expected_projection_ids = {
            context.bindings[quote]
            for quote in (*intent.measure_quotes, *intent.dimension_quotes)
            if context.bindings.get(quote) in referenced_columns
        }
        actual_projection_ids: list[str | None] = []
        for projection in root.expressions:
            expression = (
                projection.this if isinstance(projection, exp.Alias) else projection
            )
            if (
                isinstance(projection, exp.Alias)
                and isinstance(expression, exp.Column)
                and projection.alias.casefold() != expression.name.casefold()
            ):
                return _failed("projection_alias_mismatch")
            projection_id = (
                _column_id(expression, columns_by_coordinate)
                if isinstance(expression, exp.Column)
                else None
            )
            if projection_id is None and isinstance(expression, exp.Column):
                direct_lineage_ids = {
                    column.id
                    for table in context.physical_schema.tables
                    for column in table.columns
                    if column.name == expression.name
                    and column.id in referenced_columns
                }
                projection_id = (
                    direct_lineage_ids.pop() if len(direct_lineage_ids) == 1 else None
                )
            actual_projection_ids.append(projection_id)
        if not expected_projection_ids or (
            any(item is None for item in actual_projection_ids)
            or len(actual_projection_ids) != len(set(actual_projection_ids))
            or set(actual_projection_ids) != expected_projection_ids
        ):
            return _failed("projection_mismatch")
    aggregate_functions = [node.key.lower() for node in top_level_aggregates]
    if aggregate_functions and (
        intent.aggregation is None or set(aggregate_functions) != {intent.aggregation}
    ):
        return _failed("aggregation_mismatch")

    required_measure_columns = {
        context.bindings[quote]
        for quote in intent.measure_quotes
        if context.bindings.get(quote) in referenced_columns
    }
    for metric_id in required_metric_ids:
        candidate = next(
            item for item in context.candidates if item.object_id == metric_id
        )
        required_measure_columns.update(candidate.related_column_ids)
    table_count_subject = bool(required_table_ids) and intent.aggregation == "count"
    if table_count_subject and any(
        not isinstance(aggregate, exp.Count) or aggregate.find(exp.Star) is None
        for aggregate in top_level_aggregates
    ):
        return _failed("aggregation_operand_mismatch")
    for aggregate in top_level_aggregates:
        if isinstance(aggregate, exp.Count) and aggregate.find(exp.Star) is not None:
            continue
        operand = aggregate.this
        operands = (
            {_column_id(operand, columns_by_coordinate)}
            if isinstance(operand, exp.Column)
            else set()
        )
        if required_measure_columns and (
            len(operands) != 1 or not operands.issubset(required_measure_columns)
        ):
            return _failed("aggregation_operand_mismatch")
        if isinstance(operand, exp.Column):
            operand_id = _column_id(operand, columns_by_coordinate)
            operand_column = next(
                (
                    column
                    for table in context.physical_schema.tables
                    for column in table.columns
                    if column.id == operand_id
                ),
                None,
            )
            numeric_type = bool(
                operand_column
                and re.match(
                    r"^(TINYINT|SMALLINT|MEDIUMINT|INT|INTEGER|BIGINT|DECIMAL|NUMERIC|FLOAT|DOUBLE|REAL)\b",
                    operand_column.data_type.upper(),
                )
            )
            if isinstance(aggregate, (exp.Sum, exp.Avg)) and not numeric_type:
                return _failed("aggregation_type_mismatch")
    actual_aggregate_operands = [
        _column_id(node.this, columns_by_coordinate)
        for node in top_level_aggregates
        if not (isinstance(node, exp.Count) and node.find(exp.Star) is not None)
        and isinstance(node.this, exp.Column)
    ]
    if (
        top_level_aggregates
        and required_measure_columns
        and (
            len(actual_aggregate_operands) != len(required_measure_columns)
            or set(actual_aggregate_operands) != required_measure_columns
        )
    ):
        return _failed("aggregation_operand_mismatch")

    projection_aliases = {
        projection.alias: projection.this
        for projection in root.expressions
        if projection.alias
    }
    actual_sorts: list[tuple[str | None, str]] = []
    aggregate_sql = {
        aggregate.sql(dialect="mysql"): aggregate for aggregate in top_level_aggregates
    }
    order = root.args.get("order")
    if isinstance(order, exp.Order):
        for ordered in order.expressions:
            expression = ordered.this
            if isinstance(expression, exp.Column) and not expression.table:
                expression = projection_aliases.get(expression.name, expression)
            metric_id = next(
                (
                    candidate.object_id
                    for candidate in context.candidates
                    if candidate.kind == QueryMetadataKind.METRIC
                    and set(candidate.related_column_ids)
                    == {
                        _column_id(column, columns_by_coordinate)
                        for column in expression.find_all(exp.Column)
                    }
                    and isinstance(expression, exp.AggFunc)
                ),
                None,
            )
            exact_column_id = (
                _column_id(expression, columns_by_coordinate)
                if isinstance(expression, exp.Column)
                else _column_id(expression.this, columns_by_coordinate)
                if isinstance(expression, exp.AggFunc)
                and expression.sql(dialect="mysql") in aggregate_sql
                and isinstance(expression.this, exp.Column)
                else None
            )
            if exact_column_id is None and intent.grain is not None:
                time_column_id = context.bindings.get(intent.time_column_quote or "")
                group = root.args.get("group")
                if any(
                    expression.sql(dialect="mysql") == grouped.sql(dialect="mysql")
                    for grouped in (
                        group.expressions if isinstance(group, exp.Group) else []
                    )
                ) and {
                    _column_id(column, columns_by_coordinate)
                    for column in expression.find_all(exp.Column)
                } == {time_column_id}:
                    exact_column_id = time_column_id
            actual_sorts.append(
                (
                    metric_id or exact_column_id,
                    "desc" if ordered.args.get("desc") else "asc",
                )
            )
    expected_sorts = [
        (context.bindings.get(item.quote), item.direction) for item in intent.sorts
    ]
    if (
        intent.grain == "quarter"
        and len(expected_sorts) == 1
        and isinstance(order, exp.Order)
        and len(order.expressions) == 2
    ):
        quarter_order = [
            projection_aliases.get(item.this.name, item.this)
            if isinstance(item.this, exp.Column) and not item.this.table
            else item.this
            for item in order.expressions
        ]
        directions = [
            "desc" if item.args.get("desc") else "asc" for item in order.expressions
        ]
        time_column_id = context.bindings.get(intent.time_column_quote or "")
        operands = [
            item.this
            for item in quarter_order
            if isinstance(item, (exp.Year, exp.Quarter))
        ]
        if (
            isinstance(quarter_order[0], exp.Year)
            and isinstance(quarter_order[1], exp.Quarter)
            and len(operands) == 2
            and {
                _column_id(item, columns_by_coordinate)
                for item in operands
                if isinstance(item, exp.Column)
            }
            == {time_column_id}
            and directions[0] == directions[1]
        ):
            actual_sorts = [(time_column_id, directions[0])]
    if actual_sorts != expected_sorts:
        return _failed("sort_mismatch")

    group_expressions = group.expressions if isinstance(group, exp.Group) else []
    dimension_ids = {
        context.bindings[quote]
        for quote in intent.dimension_quotes
        if context.bindings.get(quote) is not None
    }
    if intent.query_type in (QueryType.COMPARISON, QueryType.RANKING):
        if any(
            not isinstance(expression, exp.Column) for expression in group_expressions
        ):
            return _failed("group_mismatch")
        actual_group_ids = {
            _column_id(expression, columns_by_coordinate)
            for expression in group_expressions
            if isinstance(expression, exp.Column)
        }
        if actual_group_ids != dimension_ids:
            return _failed("group_mismatch")

    if intent.grain is not None:
        time_column_id = context.bindings.get(intent.time_column_quote or "")
        time_column = next(
            (
                column
                for table in context.physical_schema.tables
                for column in table.columns
                if column.id == time_column_id
            ),
            None,
        )
        if time_column is None or not time_column.data_type.upper().startswith(
            ("DATE", "DATETIME", "TIMESTAMP")
        ):
            return _failed("time_type_mismatch")

        def bucket_matches(expression: exp.Expression, grain: str) -> bool:
            operand: exp.Expression | None = None
            if grain == "day" and isinstance(expression, exp.TsOrDsToDate):
                operand = expression.this
            elif (
                grain == "week"
                and isinstance(expression, exp.Anonymous)
                and expression.name.upper() == "YEARWEEK"
                and len(expression.expressions) == 1
            ):
                operand = expression.expressions[0]
            elif (
                grain == "month"
                and isinstance(expression, exp.TimeToStr)
                and isinstance(expression.args.get("format"), exp.Literal)
                and str(expression.args["format"].this) == "%Y-%m"
            ):
                operand = expression.this
            elif grain == "quarter" and isinstance(expression, exp.Quarter):
                operand = expression.this
            elif grain == "year" and isinstance(expression, exp.Year):
                operand = expression.this
                if isinstance(operand, exp.TsOrDsToDate):
                    operand = operand.this
            return (
                isinstance(operand, exp.Column)
                and _column_id(operand, columns_by_coordinate) == time_column_id
            )

        buckets = [
            item for item in group_expressions if bucket_matches(item, intent.grain)
        ]
        quarter_years = [
            item for item in group_expressions if bucket_matches(item, "year")
        ]
        bucket_count = 2 if intent.grain == "quarter" else 1
        bucket_ids = {id(item) for item in (*buckets, *quarter_years)}
        business_groups = [
            item for item in group_expressions if id(item) not in bucket_ids
        ]
        business_group_ids = {
            _column_id(item, columns_by_coordinate)
            for item in business_groups
            if isinstance(item, exp.Column)
        }
        if (
            time_column_id is None
            or len(group_expressions) != bucket_count + len(dimension_ids)
            or len(buckets) != 1
            or (intent.grain == "quarter" and len(quarter_years) != 1)
            or business_group_ids != dimension_ids
        ):
            return _failed("time_grain_mismatch")
    if intent.query_type in (QueryType.COMPARISON, QueryType.RANKING, QueryType.TREND):
        required_projection_sql = {
            expression.sql(dialect="mysql") for expression in group_expressions
        } | {aggregate.sql(dialect="mysql") for aggregate in top_level_aggregates}
        if intent.query_type == QueryType.RANKING and not group_expressions:
            expected_ranking_ids = {
                context.bindings[quote]
                for quote in (*intent.measure_quotes, *intent.dimension_quotes)
                if context.bindings.get(quote) in referenced_columns
            }
            required_projection_sql.update(
                projection.sql(dialect="mysql")
                for projection in root.expressions
                if isinstance(projection, exp.Column)
                and _column_id(projection, columns_by_coordinate)
                in expected_ranking_ids
            )
            if {
                _column_id(projection, columns_by_coordinate)
                for projection in root.expressions
                if isinstance(projection, exp.Column)
            } != expected_ranking_ids:
                return _failed("projection_mismatch")
        actual_projection_sql = []
        for projection in root.expressions:
            expression = (
                projection.this if isinstance(projection, exp.Alias) else projection
            )
            if (
                isinstance(projection, exp.Alias)
                and isinstance(expression, exp.Column)
                and projection.alias.casefold() != expression.name.casefold()
            ):
                return _failed("projection_alias_mismatch")
            actual_projection_sql.append(expression.sql(dialect="mysql"))
            if expression.sql(dialect="mysql") not in required_projection_sql:
                return _failed("projection_mismatch")
        if set(actual_projection_sql) != required_projection_sql:
            return _failed("projection_mismatch")
    validated = ValidatedQuery(
        sql=draft.sql,
        params=dict(draft.params),
        target_tables=tuple(sorted({table.name for table in physical_tables})),
        table_ids=tuple(sorted(referenced_tables)),
        column_ids=tuple(sorted(referenced_columns)),
        metric_ids=tuple(sorted(draft.metric_ids)),
        _token=_VALIDATED_TOKEN,
    )
    return QueryValidationResult(validated)
