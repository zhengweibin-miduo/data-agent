"""Qdrant Meta Dense + BM25 语义投影。"""

from uuid import NAMESPACE_URL, uuid5

from qdrant_client.async_qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    Document,
    FieldCondition,
    Filter,
    Fusion,
    FusionQuery,
    MatchAny,
    Modifier,
    PayloadIndexInfo,
    PayloadSchemaType,
    PointIdsList,
    PointStruct,
    Prefetch,
    SparseVectorParams,
    VectorParams,
)

from data_agent.errors import DataAgentError
from data_agent.metadata_indexing.models import (
    MetadataObjectKind,
    MetadataSemanticHit,
    MetadataSemanticProjection,
)
from data_agent.settings import app_config

_DENSE = "dense"
_BM25 = "bm25"
_BM25_MODEL = "Qdrant/bm25"


def metadata_point_id(kind: MetadataObjectKind, object_id: str) -> str:
    """生成稳定且与长期记忆隔离的 point UUID。"""
    return str(uuid5(NAMESPACE_URL, f"data-agent-metadata:{kind.value}:{object_id}"))


class MetadataQdrantIndex:
    """管理项目专用 Meta 语义集合。"""

    def __init__(self, client: AsyncQdrantClient) -> None:
        """绑定共享异步 Qdrant 客户端。"""
        self._client = client
        self._collection = app_config.qdrant.metadata_collection

    async def setup(self) -> None:
        """创建或严格校验 dense 与服务端 BM25 配置。"""
        payload_schema: dict[str, PayloadIndexInfo] = {}
        if not await self._client.collection_exists(self._collection):
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config={
                    _DENSE: VectorParams(
                        size=app_config.qdrant.vector_size,
                        distance=Distance(app_config.qdrant.distance),
                    )
                },
                sparse_vectors_config={
                    _BM25: SparseVectorParams(modifier=Modifier.IDF)
                },
            )
        else:
            info = await self._client.get_collection(self._collection)
            payload_schema = info.payload_schema
            vectors = info.config.params.vectors
            sparse = info.config.params.sparse_vectors or {}
            dense = vectors.get(_DENSE) if isinstance(vectors, dict) else None
            if (
                dense is None
                or dense.size != app_config.qdrant.vector_size
                or dense.distance != Distance(app_config.qdrant.distance)
                or _BM25 not in sparse
                or sparse[_BM25].modifier != Modifier.IDF
            ):
                raise DataAgentError(
                    "metadata_semantic_mapping_invalid",
                    "metadata_index_setup",
                    "既有 Meta 语义集合与当前向量或 BM25 配置不一致",
                    details={"collection": self._collection},
                )
        fields = ("kind", "object_id", "table_id", "projection_version")
        if any(
            payload_schema[field].data_type != PayloadSchemaType.KEYWORD
            for field in fields
            if field in payload_schema
        ):
            raise DataAgentError(
                "metadata_semantic_mapping_invalid",
                "metadata_index_setup",
                "既有 Meta 语义集合 payload 索引类型不一致",
                details={"collection": self._collection},
            )
        for field in fields:
            await self._client.create_payload_index(
                collection_name=self._collection,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
                wait=True,
            )

    async def recreate(self) -> None:
        """仅重建配置明确指定的 Meta 语义集合。"""
        if await self._client.collection_exists(self._collection):
            await self._client.delete_collection(self._collection)
        await self.setup()

    async def upsert(
        self,
        projection: MetadataSemanticProjection,
        vector: list[float],
    ) -> None:
        """按稳定 point ID 幂等覆盖 dense 与 BM25 文档。"""
        if len(vector) != app_config.qdrant.vector_size:
            raise ValueError("TEI embedding 维度与 Meta Qdrant 配置不一致")
        await self._client.upsert(
            collection_name=self._collection,
            points=[
                PointStruct(
                    id=metadata_point_id(projection.kind, projection.object_id),
                    vector={
                        _DENSE: vector,
                        _BM25: Document(text=projection.search_text, model=_BM25_MODEL),
                    },
                    payload=projection.model_dump(mode="json"),
                )
            ],
            wait=True,
        )

    async def delete(self, kind: MetadataObjectKind, object_id: str) -> None:
        """幂等删除 Meta point。"""
        await self._client.delete(
            collection_name=self._collection,
            points_selector=PointIdsList(points=[metadata_point_id(kind, object_id)]),
            wait=True,
        )

    async def search(
        self,
        query: str,
        vector: list[float],
        kinds: set[MetadataObjectKind] | None,
        limit: int,
    ) -> list[MetadataSemanticHit]:
        """使用 Qdrant RRF 融合 dense 与服务端 BM25 候选。"""
        if len(vector) != app_config.qdrant.vector_size:
            raise ValueError("TEI query embedding 维度与 Meta Qdrant 配置不一致")
        query_filter = (
            Filter(
                must=[
                    FieldCondition(
                        key="projection_version",
                        match=MatchAny(
                            any=[app_config.metadata_index.projection_version]
                        ),
                    ),
                    FieldCondition(
                        key="kind",
                        match=MatchAny(any=sorted(kind.value for kind in kinds)),
                    ),
                ]
            )
            if kinds
            else Filter(
                must=[
                    FieldCondition(
                        key="projection_version",
                        match=MatchAny(
                            any=[app_config.metadata_index.projection_version]
                        ),
                    )
                ]
            )
        )
        result = await self._client.query_points(
            collection_name=self._collection,
            prefetch=[
                Prefetch(query=vector, using=_DENSE, filter=query_filter, limit=limit),
                Prefetch(
                    query=Document(text=query, model=_BM25_MODEL),
                    using=_BM25,
                    filter=query_filter,
                    limit=limit,
                ),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=limit,
            with_payload=["kind", "object_id", "schema_fingerprint", "search_text"],
        )
        return [
            MetadataSemanticHit(
                kind=MetadataObjectKind(str(point.payload["kind"])),
                object_id=str(point.payload["object_id"]),
                schema_fingerprint=str(point.payload["schema_fingerprint"]),
                score=float(point.score),
                matched_text=str(point.payload.get("search_text", "")),
            )
            for point in result.points
            if point.payload
            and "kind" in point.payload
            and "object_id" in point.payload
            and "schema_fingerprint" in point.payload
        ]
