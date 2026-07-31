"""经 Meta 与 data_sync 回查的内部两阶段索引检索。"""

from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.infrastructure.tei_embeddings import TEIEmbeddingClient
from data_agent.metadata_indexing.elasticsearch import (
    MetadataValueElasticsearchIndex,
)
from data_agent.metadata_indexing.models import (
    MetadataCandidate,
    MetadataObjectKind,
    MetadataValueCandidate,
    MetadataValueProjection,
    MetadataValueSearchResult,
)
from data_agent.metadata_indexing.projections import MetadataProjectionRepository
from data_agent.metadata_indexing.qdrant import MetadataQdrantIndex
from data_agent.settings import app_config


def _bounded_limit(limit: int | None) -> int:
    """把内部检索数量限制在配置上限内。"""
    if limit is not None and limit <= 0:
        raise ValueError("检索 limit 必须为正整数")
    return min(
        limit or app_config.metadata_index.search_limit,
        app_config.metadata_index.search_limit,
    )


def _semantic_candidate_limit() -> int:
    """返回权威过滤前的有界语义候选池大小。"""
    return app_config.metadata_index.search_limit


def _value_candidate_limit() -> int:
    """返回权威过滤前的有界字段值候选池大小。"""
    return app_config.metadata_index.search_limit


def _refresh_generation_matches(
    before: dict[str, frozenset[str]],
    after: dict[str, frozenset[str]],
    projections: list[MetadataValueProjection],
) -> bool:
    """确认查询前后表级可见代次集合稳定，包含混合代次场景。"""
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
    """仅在权威范围改变时丢弃候选，不完整状态仍保留有效部分结果。"""
    if final_scope != current_scope:
        return [], False
    return values, complete and final_complete


class MetadataSearchService:
    """执行语义候选检索与候选字段范围内的值检索。"""

    async def search_metadata(
        self,
        query: str,
        kinds: set[MetadataObjectKind] | None = None,
        limit: int | None = None,
    ) -> list[MetadataCandidate]:
        """用 Qdrant 取候选，再回读 Meta 并剔除尚未收敛的对象。"""
        if not query.strip():
            raise ValueError("Meta 检索 query 不能为空")
        bounded_limit = _bounded_limit(limit)
        # 步骤一：Qdrant 只提供有序对象身份，不提供权威业务内容。
        vector = await TEIEmbeddingClient.get_client().aembed_query(query)
        identities = await MetadataQdrantIndex(QdrantClient.get_client()).search(
            query, vector, kinds, _semantic_candidate_limit()
        )
        # 步骤二：从 Meta 回读当前对象；存在 pending desired state 的候选不可用。
        async with MySQLDatabase.session() as session:
            candidates = await MetadataProjectionRepository(
                session
            ).authoritative_candidates(identities)
        return candidates[:bounded_limit]

    async def search_values(
        self,
        query: str,
        column_ids: set[str],
        limit: int | None = None,
    ) -> MetadataValueSearchResult:
        """仅在合格候选字段内检索值，并返回当前完整性。"""
        if not query.strip():
            raise ValueError("字段值检索 query 不能为空")
        if not column_ids:
            raise ValueError("字段值检索必须限定候选 column_ids")
        bounded_limit = _bounded_limit(limit)
        # 步骤一：先从 Meta 与 data_sync 解析当前合格字段范围。
        async with MySQLDatabase.session() as session:
            scope, _ = await MetadataProjectionRepository(session).resolve_value_scope(
                column_ids
            )
        if not scope:
            return MetadataValueSearchResult(values=[], complete=False)
        # 步骤二：Elasticsearch 只在解析后的字段范围内提供候选值。
        value_index = MetadataValueElasticsearchIndex(ElasticsearchClient.get_client())
        table_ids = {table_id for table_id, _ in scope.values()}
        versions_before = await value_index.current_refresh_versions(table_ids)
        projections = await value_index.search(
            query, set(scope), _value_candidate_limit()
        )
        # 步骤三：外部调用后重新解析权威范围，拒绝并发结构变更产生的旧命中。
        async with MySQLDatabase.session() as session:
            repository = MetadataProjectionRepository(session)
            current_scope, complete = await repository.resolve_value_scope(column_ids)
            values = repository.authoritative_value_candidates(
                projections,
                current_scope,
            )
        versions_after = await value_index.current_refresh_versions(
            {table_id for table_id, _ in current_scope.values()}
        )
        generation_matches = _refresh_generation_matches(
            versions_before,
            versions_after,
            projections,
        )
        if not generation_matches:
            values = []
            complete = False
        # 以最后一次权威读取作为完整性线性化点；ES 代次检查期间新投递但
        # 尚未写入索引的 generation 也必须使结果降级为不完整。
        async with MySQLDatabase.session() as session:
            final_scope, final_complete = await MetadataProjectionRepository(
                session
            ).resolve_value_scope(column_ids)
        values, complete = _finalize_value_results(
            values,
            current_scope,
            final_scope,
            complete,
            final_complete,
        )
        return MetadataValueSearchResult(
            values=values[:bounded_limit], complete=complete
        )
