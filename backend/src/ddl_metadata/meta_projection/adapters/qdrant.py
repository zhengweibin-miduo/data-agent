"""Qdrant 与 TEI 组合成语义投影端口。"""

from __future__ import annotations

from typing import Protocol

from ddl_metadata.meta_projection.models import (
    MetadataObjectKind,
    MetadataSemanticHit,
    MetadataSemanticProjection,
)
from ddl_metadata.meta_projection.qdrant import MetadataQdrantIndex


class EmbeddingProvider(Protocol):
    """语义投影所需的异步文档与查询向量 interface。"""

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        """生成一批文档向量。"""
        ...

    async def aembed_query(self, text: str) -> list[float]:
        """生成一个查询向量。"""
        ...


class QdrantSemanticIndex:
    """隐藏 TEI 向量化与 Qdrant payload 细节的语义 adapter。"""

    def __init__(
        self,
        *,
        index: MetadataQdrantIndex,
        embeddings: EmbeddingProvider,
    ) -> None:
        """绑定 Qdrant 索引与异步 embedding provider。"""
        self._index = index
        self._embeddings = embeddings

    async def setup(self) -> None:
        """创建或严格校验语义索引。"""
        await self._index.setup()

    async def recreate(self) -> None:
        """幂等重建项目语义索引。"""
        await self._index.recreate()

    async def upsert(self, projection: MetadataSemanticProjection) -> None:
        """向量化并写入一个语义投影。"""
        vectors = await self._embeddings.aembed_documents([projection.search_text])
        await self._index.upsert(projection, vectors[0])

    async def delete(self, kind: MetadataObjectKind, object_id: str) -> None:
        """删除一个语义投影。"""
        await self._index.delete(kind, object_id)

    async def search(
        self,
        query: str,
        kinds: set[MetadataObjectKind] | None,
        limit: int,
        *,
        table_ids: set[str] | None = None,
        column_ids: set[str] | None = None,
    ) -> list[MetadataSemanticHit]:
        """向量化查询并返回有界语义候选身份。"""
        vector = await self._embeddings.aembed_query(query)
        return await self._index.search(
            query,
            vector,
            kinds,
            limit,
            table_ids=table_ids,
            column_ids=column_ids,
        )
