"""Memory Projection 远程写入端口的生产适配器。"""

from typing import Protocol

from data_agent.memory.indexing.elasticsearch import MemoryElasticsearchIndex
from data_agent.memory.indexing.qdrant import MemoryQdrantIndex
from data_agent.models.memory import MemoryProjection


class DocumentEmbeddingClient(Protocol):
    """投影向量化所需的最小 TEI 客户端接口。"""

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        """批量生成文档向量。"""
        ...


class ElasticsearchMemoryProjectionIndex:
    """把 Elasticsearch mapping adapter 暴露为投影写入端口。"""

    def __init__(self, index: MemoryElasticsearchIndex) -> None:
        """绑定既有 Elasticsearch 索引适配器。"""
        self._index = index

    async def apply(
        self,
        memory_uid: str,
        projection: MemoryProjection | None,
    ) -> None:
        """按权威裁决幂等写入或删除全文文档。"""
        if projection is None:
            await self._index.delete(memory_uid)
            return
        await self._index.upsert(projection)


class QdrantMemoryProjectionIndex:
    """把 Qdrant 与 TEI 组合为向量投影写入端口。"""

    def __init__(
        self,
        index: MemoryQdrantIndex,
        embeddings: DocumentEmbeddingClient,
    ) -> None:
        """绑定既有 Qdrant mapping adapter 与长生命周期 TEI 客户端。"""
        self._index = index
        self._embeddings = embeddings

    async def apply(
        self,
        memory_uid: str,
        projection: MemoryProjection | None,
    ) -> None:
        """删除时跳过向量化，写入时生成文档向量后更新点。"""
        if projection is None:
            await self._index.delete(memory_uid)
            return
        vectors = await self._embeddings.aembed_documents([projection.memory_text])
        await self._index.upsert(projection, vectors[0])
