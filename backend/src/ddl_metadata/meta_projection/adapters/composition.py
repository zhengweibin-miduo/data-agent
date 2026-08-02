"""Meta Projection application use cases 的生产装配。"""

from dataclasses import dataclass

from elasticsearch import AsyncElasticsearch
from qdrant_client.async_qdrant_client import AsyncQdrantClient

from ddl_metadata.meta_projection.adapters.mysql import (
    MySQLProjectionReader,
    MySQLProjectionWorkStore,
)
from ddl_metadata.meta_projection.adapters.qdrant import (
    EmbeddingProvider,
    QdrantSemanticIndex,
)
from ddl_metadata.meta_projection.application.dispatcher import (
    MetadataIndexDispatcher,
)
from ddl_metadata.meta_projection.application.rebuild import (
    MetadataIndexRebuilder,
)
from ddl_metadata.meta_projection.application.search import (
    MetadataSearchService,
)
from ddl_metadata.meta_projection.elasticsearch import (
    MetadataValueElasticsearchIndex,
)
from ddl_metadata.meta_projection.qdrant import MetadataQdrantIndex
from ddl_metadata.meta_projection.value_refresh import (
    MetadataValueRefresh,
)
from settings import app_config


@dataclass(frozen=True)
class MetaProjectionRuntime:
    """共享同一组 adapters 的 Meta Projection 运行时用例集合。"""

    dispatcher: MetadataIndexDispatcher
    search: MetadataSearchService
    rebuilder: MetadataIndexRebuilder
    semantic_index: QdrantSemanticIndex
    value_index: MetadataValueElasticsearchIndex


def compose_meta_projection_runtime(
    *,
    elasticsearch: AsyncElasticsearch,
    qdrant: AsyncQdrantClient,
    embeddings: EmbeddingProvider,
) -> MetaProjectionRuntime:
    """从已初始化的长生命周期客户端装配全部 Meta Projection 用例。"""
    # 步骤一：创建进程级无状态 adapters；事务仍由 MySQL adapter 按调用短暂创建。
    work_store = MySQLProjectionWorkStore(
        generation_lock_timeout_seconds=(
            app_config.data_sync.generation_lock_timeout_seconds
        )
    )
    reader = MySQLProjectionReader()
    semantic_index = QdrantSemanticIndex(
        index=MetadataQdrantIndex(qdrant),
        embeddings=embeddings,
    )
    value_index = MetadataValueElasticsearchIndex(elasticsearch)
    # 步骤二：重建、调度与搜索共享 adapter identity，避免调用点重复读取配置
    # 或构造客户端。
    rebuilder = MetadataIndexRebuilder(
        work_store=work_store,
        reader=reader,
        semantic_index=semantic_index,
        value_index=value_index,
        projection_version=app_config.metadata_index.projection_version,
        es_index=app_config.elasticsearch.metadata_value_index,
        qdrant_collection=app_config.qdrant.metadata_collection,
    )
    dispatcher = MetadataIndexDispatcher(
        work_store=work_store,
        reader=reader,
        semantic_index=semantic_index,
        value_refresh=MetadataValueRefresh(),
        rebuilder=rebuilder,
    )
    search = MetadataSearchService(
        reader=reader,
        semantic_index=semantic_index,
        value_index=value_index,
        search_limit=app_config.metadata_index.search_limit,
    )
    return MetaProjectionRuntime(
        dispatcher=dispatcher,
        search=search,
        rebuilder=rebuilder,
        semantic_index=semantic_index,
        value_index=value_index,
    )
