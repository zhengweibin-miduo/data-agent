"""当前 DDL 作用域内的 Meta Projection 查询适配器。"""

from typing import Protocol

from ddl_metadata.meta_projection.models import (
    MetadataCandidate,
    MetadataObjectKind,
    MetadataValueSearchResult,
)
from models.physical import PhysicalSchema
from query.application.contracts import QueryClarification
from query.domain import (
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


_SLOT_LABELS = {
    "measure": "指标口径",
    "time": "时间范围",
    "dimension": "维度",
    "filter": "过滤字段",
    "sort": "排序对象",
}


class QueryMetadataAdapter:
    """以一次 Meta 召回完成权威绑定，再执行一次字段值召回。"""

    def __init__(self, search: MetadataSearchPort) -> None:
        """绑定现有 Meta Projection 搜索用例。"""
        self._search = search

    async def build_context(
        self,
        question: str,
        intent: QueryIntent,
        schema: PhysicalSchema,
    ) -> QueryContext | QueryClarification:
        """按当前 DDL allowlist 绑定槽位并构建有界查询上下文。"""
        # 步骤一：完整问题只触发一次既有 table/column/metric 混合召回。
        table_ids = {table.id for table in schema.tables}
        column_ids = {column.id for table in schema.tables for column in table.columns}
        recalled = await self._search.search_metadata(
            question, table_ids=table_ids, column_ids=column_ids
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
        # 步骤三：每个关键槽位必须唯一命中权威对象，分数不能消除歧义。
        slots: list[tuple[str, str, set[MetadataObjectKind]]] = [
            ("measure", quote, {MetadataObjectKind.METRIC, MetadataObjectKind.COLUMN})
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
        slots.extend(
            ("sort", item.quote, {MetadataObjectKind.METRIC, MetadataObjectKind.COLUMN})
            for item in intent.sorts
        )
        if not slots:
            return QueryClarification(
                slot="measure",
                quote=question,
                question="请明确要查询的指标或字段？",
            )
        bindings: dict[str, str] = {}
        retained: dict[str, MetadataCandidate] = {}
        for slot, quote, kinds in slots:
            matches = [
                candidate
                for candidate in candidates
                if candidate.kind in kinds and self._matches(quote, candidate)
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
            if len(matches) != 1:
                names = "、".join(candidate.name for candidate in matches[:3])
                suffix = f"，候选为：{names}" if names else ""
                return QueryClarification(
                    slot=slot,
                    quote=quote,
                    question=f"请明确“{quote}”对应的{_SLOT_LABELS[slot]}{suffix}？",
                )
            candidate = matches[0]
            bindings[quote] = candidate.object_id
            retained[candidate.object_id] = candidate
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
        """以原文和权威候选文本的双向包含建立确定性候选集合。"""
        normalized = quote.casefold().strip()
        if not normalized:
            return False
        return any(
            normalized in text.casefold() or text.casefold() in normalized
            for text in (candidate.name, candidate.description, candidate.matched_text)
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
            description=candidate.description,
            related_column_ids=candidate.related_column_ids,
            matched_text=candidate.matched_text,
        )
