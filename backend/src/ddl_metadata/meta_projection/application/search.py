"""候选召回与 Meta 权威回读用例。"""

from ddl_metadata.meta_projection.application.contracts import (
    ProjectionReader,
    SemanticIndex,
    ValueIndex,
)
from ddl_metadata.meta_projection.models import (
    MetadataCandidate,
    MetadataObjectKind,
    MetadataValueCandidate,
    MetadataValueProjection,
    MetadataValueSearchResult,
)


def _refresh_generation_matches(
    before: dict[str, frozenset[str]],
    after: dict[str, frozenset[str]],
    projections: list[MetadataValueProjection],
) -> bool:
    """确认查询前后表级可见代次集合稳定且覆盖全部命中。"""
    return before == after and all(
        projection.refresh_version in after.get(projection.table_id, frozenset())
        for projection in projections
    )


def _finalize_value_results(
    values: list[MetadataValueCandidate],
    current_scope: dict[str, tuple[str, str]],
    final_scope: dict[str, tuple[str, str]],
    complete: bool,
    final_complete: bool,
) -> tuple[list[MetadataValueCandidate], bool]:
    """权威范围改变时拒绝候选；普通 pending 只降低完整性。"""
    if final_scope != current_scope:
        return [], False
    return values, complete and final_complete


class MetadataSearchService:
    """以派生索引召回候选并通过 Meta 权威状态回读。"""

    def __init__(
        self,
        *,
        reader: ProjectionReader,
        semantic_index: SemanticIndex,
        value_index: ValueIndex,
        search_limit: int,
    ) -> None:
        """绑定读取与索引端口以及统一候选预算。"""
        if search_limit <= 0:
            raise ValueError("Meta 投影 search_limit 必须为正整数")
        self._reader = reader
        self._semantic_index = semantic_index
        self._value_index = value_index
        self._search_limit = search_limit

    def _bounded_limit(self, limit: int | None) -> int:
        """把返回数量限制在注入的候选预算内。"""
        if limit is not None and limit <= 0:
            raise ValueError("检索 limit 必须为正整数")
        return min(limit or self._search_limit, self._search_limit)

    async def search_metadata(
        self,
        query: str,
        kinds: set[MetadataObjectKind] | None = None,
        limit: int | None = None,
        *,
        table_ids: set[str] | None = None,
        column_ids: set[str] | None = None,
    ) -> list[MetadataCandidate]:
        """从语义索引取有界身份，再回读 Meta 权威候选。"""
        if not query.strip():
            raise ValueError("Meta 检索 query 不能为空")
        bounded_limit = self._bounded_limit(limit)
        # 步骤一：派生索引仅提供身份、分数与写入指纹。
        if table_ids is None and column_ids is None:
            identities = await self._semantic_index.search(
                query, kinds, self._search_limit
            )
        else:
            # 作用域内绑定必须看到完整候选集合，不能用全局展示 Top-K 证明唯一性。
            # 指标基数与字段数无上界关系；作用域绑定必须请求索引支持的完整
            # 结果，而不能用字段数猜测候选总量后声称唯一。
            scope_limit = 100_000
            identities = await self._semantic_index.search(
                query,
                kinds,
                scope_limit,
                table_ids=table_ids,
                column_ids=column_ids,
            )
        # 步骤二：权威 reader 剔除删除、pending 或指纹过期的对象。
        candidates = await self._reader.authoritative_candidates(
            identities, table_ids=table_ids, column_ids=column_ids
        )
        if table_ids is not None or column_ids is not None:
            return candidates
        return candidates[:bounded_limit]

    async def search_values(
        self,
        query: str,
        column_ids: set[str],
        limit: int | None = None,
    ) -> MetadataValueSearchResult:
        """在权威字段范围内召回值并检测所有可见刷新代次。"""
        if not query.strip():
            raise ValueError("字段值检索 query 不能为空")
        if not column_ids:
            raise ValueError("字段值检索必须限定候选 column_ids")
        bounded_limit = self._bounded_limit(limit)
        # 步骤一：先解析当前字段范围，再让 ES 只召回该范围内的候选。
        scope, _ = await self._reader.resolve_value_scope(column_ids)
        if not scope:
            return MetadataValueSearchResult(values=[], complete=False)
        table_ids = {table_id for table_id, _ in scope.values()}
        versions_before = await self._value_index.current_refresh_versions(table_ids)
        projections = await self._value_index.search(
            query,
            set(scope),
            self._search_limit,
        )
        # 步骤二：远程调用后重读权威范围，拒绝并发结构变化产生的旧命中。
        current_scope, complete = await self._reader.resolve_value_scope(column_ids)
        values = await self._reader.authoritative_value_candidates(
            projections,
            current_scope,
        )
        versions_after = await self._value_index.current_refresh_versions(
            {table_id for table_id, _ in current_scope.values()}
        )
        if not _refresh_generation_matches(
            versions_before,
            versions_after,
            projections,
        ):
            values = []
            complete = False
        # 步骤三：以最后一次权威读取作为完整性线性化点。
        final_scope, final_complete = await self._reader.resolve_value_scope(column_ids)
        values, complete = _finalize_value_results(
            values,
            current_scope,
            final_scope,
            complete,
            final_complete,
        )
        return MetadataValueSearchResult(
            values=values[:bounded_limit],
            complete=complete,
        )
