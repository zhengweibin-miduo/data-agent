"""当前 DDL 作用域内的 Meta Projection 查询适配器。"""

from typing import Protocol

from ddl_metadata.meta_projection.models import (
    MetadataCandidate,
    MetadataObjectKind,
    MetadataValueSearchResult,
)
from errors import DataAgentError
from models.physical import PhysicalSchema
from query.application.contracts import (
    QueryBindingRuleCandidate,
    QueryBindingRuleRecall,
    QueryClarification,
    QueryRuleMatcherPort,
)
from query.domain import (
    QueryBindingRuleProof,
    QueryContext,
    QueryIntent,
    QueryMetadataCandidate,
    QueryMetadataKind,
    QueryMetadataValue,
)


class MetadataSearchPort(Protocol):
    """Query 需要的现有 Meta Projection 最小搜索面。"""

    async def search_metadata(
        self,
        query: str,
        *,
        table_ids: set[str],
        column_ids: set[str],
    ) -> list[MetadataCandidate]:
        """执行一次表、字段和指标混合召回。"""
        ...

    async def search_values(
        self,
        query: str,
        column_ids: set[str],
    ) -> MetadataValueSearchResult:
        """在确定字段范围后召回值提示。"""
        ...

    async def schema_is_authoritative(
        self,
        source: str,
        schema_fingerprint: str,
        *,
        table_ids: set[str],
        column_ids: set[str],
    ) -> bool:
        """确认请求 DDL 的完整结构指纹来自 accepted snapshot。"""
        ...


_SLOT_LABELS = {
    "measure": "指标口径",
    "time": "时间范围",
    "dimension": "维度",
    "filter": "过滤字段",
    "sort": "排序对象",
}
_SEMANTIC_RULE_SIGNALS = {"elasticsearch", "qdrant"}


class QueryMetadataAdapter:
    """以一次 Meta 召回完成权威绑定，再执行一次字段值召回。"""

    def __init__(
        self, search: MetadataSearchPort, matcher: QueryRuleMatcherPort | None = None
    ) -> None:
        """绑定现有 Meta Projection 搜索用例。"""
        self._search = search
        self._matcher = matcher

    async def relationships_are_authoritative(self, schema: PhysicalSchema) -> bool:
        """重新核验请求物理模式仍是当前 accepted snapshot。"""
        return await self._search.schema_is_authoritative(
            schema.source,
            schema.schema_fingerprint,
            table_ids={table.id for table in schema.tables},
            column_ids={
                column.id for table in schema.tables for column in table.columns
            },
        )

    async def bindings_are_authoritative(self, context: QueryContext) -> bool:
        """重新召回并证明每个已选语义对象仍是唯一相同候选。"""
        schema = context.physical_schema
        table_ids = {table.id for table in schema.tables}
        column_ids = {column.id for table in schema.tables for column in table.columns}
        binding_texts = {
            quote: proof.target for quote, proof in context.rule_proofs.items()
        }
        candidates = await self._search.search_metadata(
            " ".join(binding_texts.get(quote, quote) for quote in context.bindings),
            table_ids=table_ids,
            column_ids=column_ids,
        )
        return all(
            [
                candidate.object_id
                for candidate in candidates
                if self._in_scope(candidate, table_ids, column_ids)
                and candidate.kind.value == context.binding_kinds.get(quote)
                and self._matches(
                    binding_texts.get(quote, quote),
                    candidate,
                )
            ]
            == [object_id]
            for quote, object_id in context.bindings.items()
        )

    async def build_context(
        self,
        question: str,
        intent: QueryIntent,
        schema: PhysicalSchema,
        *,
        rule_recall: QueryBindingRuleRecall | None = None,
    ) -> QueryContext | QueryClarification:
        """按当前 DDL allowlist 绑定槽位并构建有界查询上下文。"""
        # 步骤一：完整问题只触发一次既有 table/column/metric 混合召回。
        table_ids = {table.id for table in schema.tables}
        column_ids = {column.id for table in schema.tables for column in table.columns}
        rules = rule_recall or QueryBindingRuleRecall(candidates=[])
        recall_query = " ".join(
            [question, *(rule.target for rule in rules.candidates)]
        ).strip()
        recalled = await self._search.search_metadata(
            recall_query, table_ids=table_ids, column_ids=column_ids
        )
        relationships_authoritative = await self._search.schema_is_authoritative(
            schema.source,
            schema.schema_fingerprint,
            table_ids=table_ids,
            column_ids=column_ids,
        )
        if not relationships_authoritative:
            raise DataAgentError(
                "query_schema_changed",
                "query_metadata",
                "请求物理模式不是当前权威快照，请重试",
                retryable=True,
                http_status=409,
            )
        candidates = [
            candidate
            for candidate in recalled
            if self._in_scope(candidate, table_ids, column_ids)
        ]
        # 步骤二：模型已声明的歧义按固定影响顺序一次只返回一个。
        for slot in ("measure", "time", "dimension", "filter", "sort"):
            ambiguity = next(
                (item for item in intent.ambiguities if item.slot == slot),
                None,
            )
            if ambiguity is not None:
                return QueryClarification(
                    slot=slot,
                    quote=ambiguity.quote,
                    question=ambiguity.question,
                )
        if intent.time_quote and not intent.time_column_quote:
            return QueryClarification(
                slot="time",
                quote=intent.time_quote,
                question=f"请明确“{intent.time_quote}”使用哪个时间字段？",
            )
        if intent.aggregation == "count" and not intent.measure_quotes:
            return QueryClarification(
                slot="measure",
                quote=question,
                question="请明确要计数的业务主体或字段？",
            )
        # 步骤三：每个关键槽位必须唯一命中权威对象，分数不能消除歧义。
        measure_kinds = {MetadataObjectKind.METRIC, MetadataObjectKind.COLUMN}
        if intent.aggregation == "count":
            measure_kinds.add(MetadataObjectKind.TABLE)
        slots: list[tuple[str, str, set[MetadataObjectKind]]] = [
            ("measure", quote, measure_kinds)
            for quote in intent.measure_quotes
        ]
        if intent.time_column_quote:
            slots.append(
                ("time", intent.time_column_quote, {MetadataObjectKind.COLUMN})
            )
        slots.extend(
            ("dimension", quote, {MetadataObjectKind.COLUMN})
            for quote in intent.dimension_quotes
        )
        slots.extend(
            ("filter", item.column_quote, {MetadataObjectKind.COLUMN})
            for item in intent.filters
        )
        if intent.time_filter is not None:
            slots.append(
                (
                    "filter",
                    intent.time_filter.column_quote,
                    {MetadataObjectKind.COLUMN},
                )
            )
        sort_kinds = {MetadataObjectKind.METRIC, MetadataObjectKind.COLUMN}
        if intent.aggregation == "count":
            sort_kinds.add(MetadataObjectKind.TABLE)
        slots.extend(("sort", item.quote, sort_kinds) for item in intent.sorts)
        if not slots:
            return QueryClarification(
                slot="measure",
                quote=question,
                question="请明确要查询的指标或字段？",
            )
        bindings: dict[str, str] = {}
        rule_proofs: dict[str, QueryBindingRuleProof] = {}
        retained: dict[str, MetadataCandidate] = {}
        unresolved: list[tuple[str, str, set[MetadataObjectKind]]] = []
        normal_by_quote: dict[str, list[MetadataCandidate]] = {}
        for slot, quote, kinds in slots:
            matches = [
                candidate
                for candidate in candidates
                if candidate.kind in kinds and self._matches(quote, candidate)
            ]
            # Meta 指标只有自然语言定义和相关字段提示；多个相关字段无法证明
            # 公式、过滤口径或运算顺序，必须回到具体物理字段澄清。
            # 自然语言指标定义不是可执行公式；即使只关联一个字段，也不能证明
            # DISTINCT、条件口径或聚合函数。当前一律回到物理字段澄清。
            matches = [
                candidate
                for candidate in matches
                if candidate.kind != MetadataObjectKind.METRIC
            ]
            # 物理字段同名是无需依赖截断语义召回即可证明的歧义。
            if slot != "measure":
                exact_column_ids = {
                    column.id
                    for table in schema.tables
                    for column in table.columns
                    if column.name.casefold() == quote.casefold().strip()
                }
                if len(exact_column_ids) > 1:
                    matches = [
                        candidate
                        for candidate in candidates
                        if candidate.object_id in exact_column_ids
                    ]
            normal_by_quote[quote] = matches
            if len(matches) == 1:
                candidate = matches[0]
                bindings[quote] = candidate.object_id
                retained[candidate.object_id] = candidate
            else:
                unresolved.append((slot, quote, kinds))

        selected_rules: dict[str, QueryBindingRuleCandidate] = {}
        semantic_quotes: list[str] = []
        semantic_rules = [
            rule
            for rule in rules.candidates
            if _SEMANTIC_RULE_SIGNALS.intersection(rule.signals)
        ]
        semantic_by_uid = {rule.memory_uid: rule for rule in semantic_rules}
        for _slot, quote, _kinds in unresolved:
            exact = [
                rule
                for rule in rules.candidates
                if rule.alias.casefold().strip() == quote.casefold().strip()
            ]
            if len(exact) == 1:
                selected_rules[quote] = exact[0]
            elif exact:
                continue
            else:
                semantic_quotes.append(quote)
        if semantic_quotes and rules.degraded_targets:
            raise DataAgentError(
                "query_rule_unavailable",
                "query_rule_recall",
                "查询规则召回暂不可用，请重试",
                retryable=True,
                http_status=503,
            )
        if semantic_quotes and semantic_rules and self._matcher is not None:
            decisions = await self._matcher.match(semantic_quotes, semantic_rules)
            seen: set[str] = set()
            for decision in decisions:
                if (
                    decision.slot_quote not in semantic_quotes
                    or decision.memory_uid not in semantic_by_uid
                    or decision.slot_quote in seen
                ):
                    raise DataAgentError(
                        "query_rule_match_invalid",
                        "query_rule_match",
                        "查询规则匹配结果无效",
                        http_status=502,
                    )
                seen.add(decision.slot_quote)
                selected_rules[decision.slot_quote] = semantic_by_uid[
                    decision.memory_uid
                ]

        for slot, quote, kinds in unresolved:
            rule = selected_rules.get(quote)
            target_matches = (
                []
                if rule is None
                else [
                    candidate
                    for candidate in candidates
                    if candidate.kind in kinds
                    and candidate.kind != MetadataObjectKind.METRIC
                    and self._matches(rule.target, candidate)
                ]
            )
            if len(target_matches) != 1:
                matches = normal_by_quote[quote]
                names = "、".join(candidate.name for candidate in matches[:3])
                suffix = f"，候选为：{names}" if names else ""
                return QueryClarification(
                    slot=slot,
                    quote=quote,
                    question=f"请明确“{quote}”对应的{_SLOT_LABELS[slot]}{suffix}？",
                )
            candidate = target_matches[0]
            assert rule is not None
            bindings[quote] = candidate.object_id
            retained[candidate.object_id] = candidate
            rule_proofs[quote] = QueryBindingRuleProof(
                target=rule.target,
                memory_uid=rule.memory_uid,
                record_version=rule.record_version,
                content_hash=rule.content_hash,
            )
        # 步骤四：指标关联字段和已绑定字段共同限定唯一一次值召回。
        value_column_ids = {
            object_id for object_id in bindings.values() if object_id in column_ids
        }
        value_column_ids.update(
            column_id
            for candidate in retained.values()
            for column_id in candidate.related_column_ids
            if column_id in column_ids
        )
        if value_column_ids:
            value_result = await self._search.search_values(question, value_column_ids)
        else:
            value_result = MetadataValueSearchResult(values=[], complete=False)
        return QueryContext(
            physical_schema=schema,
            relationships_authoritative=relationships_authoritative,
            candidates=[self._candidate(candidate) for candidate in retained.values()],
            values=[
                QueryMetadataValue(
                    column_id=value.column_id,
                    table_id=value.table_id,
                    value=value.value,
                    frequency=value.frequency,
                )
                for value in value_result.values
            ],
            value_search_complete=value_result.complete,
            bindings=bindings,
            binding_kinds={
                quote: retained[object_id].kind.value
                for quote, object_id in bindings.items()
            },
            rule_proofs=rule_proofs,
        )

    @staticmethod
    def _in_scope(
        candidate: MetadataCandidate,
        table_ids: set[str],
        column_ids: set[str],
    ) -> bool:
        """拒绝不属于当前 DDL 的跨来源候选。"""
        if candidate.kind == MetadataObjectKind.TABLE:
            return candidate.object_id in table_ids
        if candidate.kind == MetadataObjectKind.COLUMN:
            return candidate.object_id in column_ids and candidate.table_id in table_ids
        return candidate.table_id in table_ids and set(
            candidate.related_column_ids
        ).issubset(column_ids)

    @staticmethod
    def _matches(quote: str, candidate: MetadataCandidate) -> bool:
        """仅以权威名称或显式召回别名的精确文本建立候选集合。"""
        normalized = quote.casefold().strip()
        if not normalized:
            return False
        return any(
            normalized == text.casefold().strip()
            for text in (candidate.name, *candidate.aliases)
            if text.strip()
        )

    @staticmethod
    def _candidate(candidate: MetadataCandidate) -> QueryMetadataCandidate:
        """把 Meta 所有者模型转换为 Query 自有的中立契约。"""
        return QueryMetadataCandidate(
            kind=QueryMetadataKind(candidate.kind.value),
            object_id=candidate.object_id,
            table_id=candidate.table_id,
            name=candidate.name,
            aliases=candidate.aliases,
            description=candidate.description,
            related_column_ids=candidate.related_column_ids,
            matched_text=candidate.matched_text,
        )
