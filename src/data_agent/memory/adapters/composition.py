"""Long-term Memory API 运行时的生产组合工厂。"""

from dataclasses import dataclass

from elasticsearch import AsyncElasticsearch
from qdrant_client.async_qdrant_client import AsyncQdrantClient

from data_agent.memory.adapters.mysql import (
    MemoryReferenceValidator,
    MySQLMemoryMaintenanceStore,
    MySQLMemoryProjectionWorkStore,
    MySQLMemorySearchStore,
    MySQLMemoryStore,
)
from data_agent.memory.adapters.projection_indexes import (
    DocumentEmbeddingClient,
    ElasticsearchMemoryProjectionIndex,
    QdrantMemoryProjectionIndex,
)
from data_agent.memory.adapters.search_indexes import (
    ElasticsearchMemoryIndex,
    QdrantMemoryIndex,
    TEIEmbeddingProvider,
)
from data_agent.memory.application.contracts import (
    MemoryMutationLeaseProvider,
    MemoryProjectionDispatchConfig,
    MemorySearchConfig,
    MemoryServiceConfig,
)
from data_agent.memory.application.index_dispatcher import MemoryIndexDispatcher
from data_agent.memory.application.maintenance import MemoryMaintenance
from data_agent.memory.application.search import MemorySearchService
from data_agent.memory.application.service import MemoryService
from data_agent.memory.indexing.elasticsearch import MemoryElasticsearchIndex
from data_agent.memory.indexing.qdrant import MemoryQdrantIndex
from data_agent.models.memory import MemoryIndexTarget
from data_agent.settings import app_config


@dataclass(frozen=True)
class MemoryRuntime:
    """API 组合根共享的 Long-term Memory 用例集合。"""

    service: MemoryService
    search: MemorySearchService


@dataclass(frozen=True)
class MemoryWorkerRuntime:
    """Worker 组合根共享的投影调度与生命周期维护用例。"""

    dispatcher: MemoryIndexDispatcher
    maintenance: MemoryMaintenance


def build_memory_runtime(
    leases: MemoryMutationLeaseProvider,
    references: MemoryReferenceValidator,
    *,
    elasticsearch: AsyncElasticsearch,
    qdrant: AsyncQdrantClient,
    embeddings: DocumentEmbeddingClient,
) -> MemoryRuntime:
    """用已初始化的外部资源构造 Long-term Memory API 用例。"""
    # 步骤一：把外部客户端和 MySQL repositories 收拢到生产 adapters。
    search_store = MySQLMemorySearchStore()
    search = MemorySearchService(
        search_store,
        ElasticsearchMemoryIndex(MemoryElasticsearchIndex(elasticsearch)),
        QdrantMemoryIndex(MemoryQdrantIndex(qdrant)),
        TEIEmbeddingProvider(embeddings),
        MemorySearchConfig(
            search_limit=app_config.memory.search_limit,
            lexical_top_k=app_config.elasticsearch.top_k,
            vector_top_k=app_config.qdrant.top_k,
            timeout_seconds=app_config.memory.retrieval_timeout_seconds,
            rrf_constant=app_config.memory.rrf_constant,
        ),
    )
    # 步骤二：复用同一 search 用例，并把变更版本作为显式值注入 application。
    service = MemoryService(
        MySQLMemoryStore(references),
        search,
        leases,
        MemoryServiceConfig(projection_version=app_config.memory.projection_version),
    )
    return MemoryRuntime(service=service, search=search)


def build_memory_worker_runtime(
    *,
    elasticsearch_client: AsyncElasticsearch,
    qdrant_client: AsyncQdrantClient,
    embeddings: DocumentEmbeddingClient,
) -> MemoryWorkerRuntime:
    """用已初始化的 MySQL、ES、Qdrant 与 TEI 资源构造 worker 用例。"""
    # 步骤一：每个远程目标只构造一个长生命周期 adapter 实例。
    elasticsearch = ElasticsearchMemoryProjectionIndex(
        MemoryElasticsearchIndex(elasticsearch_client)
    )
    qdrant = QdrantMemoryProjectionIndex(
        MemoryQdrantIndex(qdrant_client),
        embeddings,
    )
    # 步骤二：显式注入调度预算，application 层不读取全局 settings。
    dispatcher = MemoryIndexDispatcher(
        MySQLMemoryProjectionWorkStore(),
        {
            MemoryIndexTarget.ELASTICSEARCH: elasticsearch,
            MemoryIndexTarget.QDRANT: qdrant,
        },
        MemoryProjectionDispatchConfig(
            batch_size=app_config.memory.outbox_batch_size,
            max_backoff_seconds=app_config.memory.outbox_max_backoff_seconds,
        ),
    )
    return MemoryWorkerRuntime(
        dispatcher=dispatcher,
        maintenance=MemoryMaintenance(MySQLMemoryMaintenanceStore()),
    )
