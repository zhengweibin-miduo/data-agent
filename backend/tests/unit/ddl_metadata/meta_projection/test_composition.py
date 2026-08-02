"""Meta Projection 外层运行时装配测试。"""

from typing import cast

from elasticsearch import AsyncElasticsearch
from qdrant_client.async_qdrant_client import AsyncQdrantClient

from ddl_metadata.meta_projection.adapters.composition import (
    compose_meta_projection_runtime,
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
from infrastructure.tei_embeddings import TEIEmbeddings


class _Embeddings:
    """提供装配所需的最小异步 embedding interface。"""

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        """返回固定文档向量。"""
        return [[0.0] for _ in texts]

    async def aembed_query(self, text: str) -> list[float]:
        """返回固定查询向量。"""
        del text
        return [0.0]


def test_composition_constructs_all_application_use_cases() -> None:
    """一个 composition 调用构造 dispatcher、search 与 rebuild 用例。"""
    runtime = compose_meta_projection_runtime(
        elasticsearch=cast(AsyncElasticsearch, object()),
        qdrant=cast(AsyncQdrantClient, object()),
        embeddings=cast(TEIEmbeddings, _Embeddings()),
    )

    assert isinstance(runtime.dispatcher, MetadataIndexDispatcher)
    assert isinstance(runtime.search, MetadataSearchService)
    assert isinstance(runtime.rebuilder, MetadataIndexRebuilder)
