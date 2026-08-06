"""自然语言查询的纯领域契约与 SQL 安全规则。"""

import asyncio
import re
from dataclasses import InitVar, dataclass
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
    value_quotes: list[str] = Field(
        min_length=1,
        max_length=100,
        description="过滤值的用户原文列表。",
    )


class SortIntent(ContractModel):
    """完全由用户原文表达的一项排序。"""

    quote: str = Field(min_length=1, max_length=256, description="排序对象原文。")
    direction: Literal["asc", "desc"] = Field(description="用户明确表达的排序方向。")


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
            *(quote for item in self.filters for quote in item.value_quotes),
            *(item.quote for item in self.sorts),
            *(item.quote for item in self.ambiguities),
            *([self.limit_quote] if self.limit_quote else []),
        ]
        if any(
            not quote or not any(quote in message for message in user_messages)
            for quote in quotes
        ):
            raise ValueError("QueryIntent 关键短语必须逐字来自用户原文")
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
        if (self.aggregation is None) != (self.aggregation_quote is None):
            raise ValueError("聚合运算必须携带用户原文证据")
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
        if self.time_quote and self.time_filter is None:
            raise ValueError("时间范围必须提供可验证的过滤契约")
        if self.grain and (self.time_column_quote is None or self.time_filter is None):
            raise ValueError("时间粒度必须提供时间字段和范围契约")


QueryParameter = str | int | float | bool | None


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
    operator: str, values: list[QueryParameter], quotes: list[str]
) -> bool:
    """按用户证据精确比较谓词绑定值。"""
    normalized = [str(value) for value in values]
    if operator == "contains":
        normalized = [value.removeprefix("%").removesuffix("%") for value in normalized]
    return normalized == quotes


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
    if intent.grain and (
        intent.time_column_quote is None or intent.time_filter is None
    ):
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

    # 步骤二：真实物理表必须显式位于 DW schema，并来自当前 DDL allowlist。
    tables_by_name = {table.name: table for table in context.physical_schema.tables}
    physical_tables = [
        table
        for scope in traverse_scope(root)
        for table in _physical_sources(scope).values()
    ]
    for table_node in physical_tables:
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
    table_by_qualified = {
        table.qualified_name: table for table in context.physical_schema.tables
    }
    table_by_qualified.update(tables_by_name)
    for relation in context.physical_schema.relationships:
        target_table = table_by_qualified.get(relation.target_table)
        if target_table is None:
            continue
        target_column = next(
            (
                column
                for column in target_table.columns
                if column.name == relation.target_column
            ),
            None,
        )
        if target_column is not None:
            fk_edges.add(frozenset((relation.source_column_id, target_column.id)))
    aliases = {
        table.alias_or_name: tables_by_name[table.name] for table in physical_tables
    }
    columns_by_coordinate = {
        (alias, column.name): column.id
        for alias, table in aliases.items()
        for column in table.columns
    }
    for join in root.find_all(exp.Join):
        if join.kind.upper() == "CROSS" or join.args.get("on") is None:
            return _failed("join_forbidden")
        on = join.args["on"]
        if on.find(exp.Or) is not None:
            return _failed("join_unsupported")
        equalities = list(on.find_all(exp.EQ))
        if any(
            not isinstance(node, (exp.And, exp.EQ, exp.Column, exp.Identifier))
            for node in on.walk()
        ):
            return _failed("join_unsupported")
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
    if not required_metric_ids.issubset(draft.metric_ids):
        return _failed("binding_missing")
    if not required_table_ids.issubset(referenced_tables):
        return _failed("binding_missing")
    if not required_column_ids.issubset(referenced_columns):
        return _failed("binding_missing")
    predicate_nodes = [
        node
        for clause in (*root.find_all(exp.Where), *root.find_all(exp.Having))
        for node in clause.this.walk()
        if isinstance(
            node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Like)
        )
    ]
    predicate_contracts = [
        _predicate_contract(node, columns_by_coordinate, draft.params)
        for node in predicate_nodes
    ]
    if any(
        clause.this.find(exp.Not) is not None
        for clause in (*root.find_all(exp.Where), *root.find_all(exp.Having))
    ):
        return _failed("predicate_mismatch")
    expected_filters = [*intent.filters]
    if intent.time_filter is not None:
        expected_filters.append(intent.time_filter)
    expected_contracts = [
        (
            context.bindings.get(item.column_quote),
            item.operator,
            item.value_quotes,
        )
        for item in expected_filters
    ]
    if len(predicate_contracts) != len(expected_contracts) or any(
        actual is None
        or expected[0] is None
        or actual[0] != expected[0]
        or actual[1] != expected[1]
        or not _values_match(actual[1], actual[2], expected[2])
        for actual, expected in zip(
            predicate_contracts, expected_contracts, strict=True
        )
    ):
        return _failed("predicate_mismatch")

    # 步骤九：查询形态、时间粒度和排序必须与可信 QueryIntent 精确一致。
    has_aggregate = root.find(exp.AggFunc) is not None
    has_group = root.args.get("group") is not None
    shape_valid = {
        QueryType.DETAIL: not has_aggregate and not has_group,
        QueryType.AGGREGATE: has_aggregate,
        QueryType.RANKING: limit is not None,
        QueryType.TREND: has_aggregate and has_group,
        QueryType.COMPARISON: has_aggregate and has_group,
    }[intent.query_type]
    if not shape_valid:
        return _failed("query_shape_mismatch")
    aggregate_functions = [node.key.lower() for node in root.find_all(exp.AggFunc)]
    if aggregate_functions and (
        intent.aggregation is None
        or set(aggregate_functions) != {intent.aggregation}
    ):
        return _failed("aggregation_mismatch")

    projection_aliases = {
        projection.alias: next(projection.find_all(exp.Column), None)
        for projection in root.expressions
        if projection.alias
    }
    actual_sorts: list[tuple[str | None, str]] = []
    order = root.args.get("order")
    if isinstance(order, exp.Order):
        for ordered in order.expressions:
            expression = ordered.this
            if isinstance(expression, exp.Column) and not expression.table:
                expression = projection_aliases.get(expression.name, expression)
            actual_sorts.append(
                (
                    _column_id(expression, columns_by_coordinate)
                    if isinstance(expression, exp.Column)
                    else None,
                    "desc" if ordered.args.get("desc") else "asc",
                )
            )
    expected_sorts = [
        (context.bindings.get(item.quote), item.direction) for item in intent.sorts
    ]
    if actual_sorts != expected_sorts:
        return _failed("sort_mismatch")

    if intent.grain is not None:
        time_column_id = context.bindings.get(intent.time_column_quote or "")
        group = root.args.get("group")
        grouped_column_ids = {
            _column_id(column, columns_by_coordinate)
            for expression in (
                group.expressions if isinstance(group, exp.Group) else []
            )
            for column in expression.find_all(exp.Column)
        }
        group_sql = " ".join(
            expression.sql(dialect="mysql").upper()
            for expression in (
                group.expressions if isinstance(group, exp.Group) else []
            )
        )
        grain_markers = {
            "day": "DATE(",
            "week": "YEARWEEK(",
            "month": "%Y-%M",
            "quarter": "QUARTER(",
            "year": "YEAR(",
        }
        if (
            time_column_id is None
            or time_column_id not in grouped_column_ids
            or grain_markers[intent.grain] not in group_sql
            or (intent.grain == "quarter" and "YEAR(" not in group_sql)
        ):
            return _failed("time_grain_mismatch")
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
