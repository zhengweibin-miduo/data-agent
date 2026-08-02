"""Memory 检索端口的外部索引生产适配器。"""

from collections.abc import Sequence

from memory.indexing.elasticsearch import MemoryElasticsearchIndex
from memory.indexing.qdrant import MemoryQdrantIndex


class ElasticsearchMemoryIndex:
    """把 Elasticsearch mapping adapter 暴露为词法检索端口。"""

    def __init__(self, index: MemoryElasticsearchIndex) -> None:
        """绑定既有 Elasticsearch 索引适配器。"""
        self._index = index

    async def search(
        self,
        query: str,
        source: str,
        categories: set[str] | None,
        limit: int,
        *,
        user_id: str | None,
    ) -> list[str]:
        """返回 BM25 候选 UID。"""
        return await self._index.search(
            query, source, categories, limit, user_id=user_id
        )


class QdrantMemoryIndex:
    """把 Qdrant mapping adapter 暴露为向量检索端口。"""

    def __init__(self, index: MemoryQdrantIndex) -> None:
        """绑定既有 Qdrant 索引适配器。"""
        self._index = index

    async def search(
        self,
        vector: Sequence[float],
        source: str,
        categories: set[str] | None,
        limit: int,
        *,
        user_id: str | None,
    ) -> list[str]:
        """返回向量候选 UID。"""
        return await self._index.search(
            list(vector), source, categories, limit, user_id=user_id
        )


class TEIEmbeddingProvider:
    """把 TEI 客户端暴露为查询向量端口。"""

    def __init__(self, client: object) -> None:
        """绑定支持 ``aembed_query`` 的 TEI 客户端。"""
        self._client = client

    async def embed_query(self, query: str) -> list[float]:
        """生成查询向量。"""
        method = getattr(self._client, "aembed_query")
        result: list[float] = await method(query)
        return result
