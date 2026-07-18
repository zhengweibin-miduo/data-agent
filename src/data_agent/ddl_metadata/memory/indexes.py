"""Elasticsearch BM25 与 Qdrant 向量记忆投影。"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from elasticsearch import AsyncElasticsearch, NotFoundError
from qdrant_client.async_qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Condition,
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PayloadSchemaType,
    PointIdsList,
    PointStruct,
    VectorParams,
)

from data_agent.ddl_metadata.models import MemoryKind, MemoryProjection
from data_agent.settings import app_config


def _qdrant_point_id(memory_uid: str) -> str:
    """把 64 位内容 UID 映射为 Qdrant 支持的稳定 UUID。"""
    return str(uuid5(NAMESPACE_URL, f"data-agent-memory:{memory_uid}"))


def _filter_conditions(
    source: str,
    kinds: set[MemoryKind] | None,
) -> list[Condition]:
    """构造两个索引共享的严格作用域条件。"""
    conditions: list[Condition] = [
        FieldCondition(key="source", match=MatchValue(value=source)),
        FieldCondition(key="status", match=MatchValue(value="ACTIVE")),
        FieldCondition(
            key="content_version",
            match=MatchValue(value=app_config.memory.content_version),
        ),
        FieldCondition(
            key="projection_version",
            match=MatchValue(value=app_config.memory.projection_version),
        ),
    ]
    if kinds:
        conditions.append(
            FieldCondition(
                key="kind",
                match=MatchAny(any=[kind.value for kind in sorted(kinds)]),
            )
        )
    return conditions


class MemoryElasticsearchIndex:
    """管理项目专用 BM25 记忆索引。"""

    def __init__(self, client: AsyncElasticsearch) -> None:
        """绑定共享异步客户端。"""
        self._client = client
        self._index = app_config.elasticsearch.memory_index

    async def setup(self) -> None:
        """幂等创建当前投影版本的映射。"""
        if await self._client.indices.exists(index=self._index):
            return
        await self._client.indices.create(
            index=self._index,
            settings={
                "analysis": {
                    "analyzer": {
                        "memory_zh": {
                            "type": "custom",
                            "tokenizer": app_config.elasticsearch.analyzer,
                        }
                    }
                }
            },
            mappings={
                "dynamic": "strict",
                "properties": {
                    "memory_text": {"type": "text", "analyzer": "memory_zh"},
                    **{
                        field: {"type": "keyword"}
                        for field in (
                            "memory_uid",
                            "source",
                            "kind",
                            "scope_key",
                            "schema_fingerprint",
                            "object_ids",
                            "trust",
                            "status",
                            "content_hash",
                            "content_version",
                            "projection_version",
                        )
                    },
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                },
            },
        )

    async def recreate(self) -> None:
        """只重建项目配置的专用索引。"""
        if await self._client.indices.exists(index=self._index):
            await self._client.indices.delete(index=self._index)
        await self.setup()

    async def upsert(self, projection: MemoryProjection) -> None:
        """按稳定 UID 幂等覆盖文档。"""
        await self._client.index(
            index=self._index,
            id=projection.memory_uid,
            document=projection.model_dump(mode="json"),
            refresh=False,
        )

    async def delete(self, memory_uid: str) -> None:
        """幂等删除指定记忆文档。"""
        try:
            await self._client.delete(
                index=self._index,
                id=memory_uid,
                refresh=False,
            )
        except NotFoundError:
            return

    async def search(
        self,
        query: str,
        source: str,
        kinds: set[MemoryKind] | None,
        limit: int,
    ) -> list[str]:
        """在严格作用域过滤后执行 BM25 检索。"""
        filters: list[dict[str, object]] = [
            {"term": {"source": source}},
            {"term": {"status": "ACTIVE"}},
            {"term": {"content_version": app_config.memory.content_version}},
            {"term": {"projection_version": app_config.memory.projection_version}},
        ]
        if kinds:
            filters.append({"terms": {"kind": [kind.value for kind in kinds]}})
        response = await self._client.search(
            index=self._index,
            size=limit,
            query={
                "bool": {
                    "filter": filters,
                    "must": {"match": {"memory_text": {"query": query}}},
                }
            },
            source=False,
        )
        return [str(hit["_id"]) for hit in response["hits"]["hits"]]


class MemoryQdrantIndex:
    """管理项目专用 Qdrant 记忆 collection。"""

    def __init__(self, client: AsyncQdrantClient) -> None:
        """绑定共享异步客户端。"""
        self._client = client
        self._collection = app_config.qdrant.memory_collection

    async def setup(self) -> None:
        """幂等创建 collection 与过滤 payload 索引。"""
        if not await self._client.collection_exists(self._collection):
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(
                    size=app_config.qdrant.vector_size,
                    distance=Distance(app_config.qdrant.distance),
                ),
            )
        for field in (
            "source",
            "kind",
            "status",
            "content_version",
            "projection_version",
        ):
            await self._client.create_payload_index(
                collection_name=self._collection,
                field_name=field,
                field_schema=PayloadSchemaType.KEYWORD,
                wait=True,
            )

    async def recreate(self) -> None:
        """只重建项目配置的专用 collection。"""
        if await self._client.collection_exists(self._collection):
            await self._client.delete_collection(self._collection)
        await self.setup()

    async def upsert(
        self,
        projection: MemoryProjection,
        vector: list[float],
    ) -> None:
        """校验向量维度后按稳定点 ID 幂等覆盖。"""
        if len(vector) != app_config.qdrant.vector_size:
            raise ValueError("TEI embedding 维度与 Qdrant 配置不一致")
        await self._client.upsert(
            collection_name=self._collection,
            points=[
                PointStruct(
                    id=_qdrant_point_id(projection.memory_uid),
                    vector=vector,
                    payload=projection.model_dump(mode="json"),
                )
            ],
            wait=True,
        )

    async def delete(self, memory_uid: str) -> None:
        """幂等删除指定记忆 point。"""
        await self._client.delete(
            collection_name=self._collection,
            points_selector=PointIdsList(points=[_qdrant_point_id(memory_uid)]),
            wait=True,
        )

    async def search(
        self,
        vector: list[float],
        source: str,
        kinds: set[MemoryKind] | None,
        limit: int,
    ) -> list[str]:
        """在严格作用域过滤后执行向量检索。"""
        if len(vector) != app_config.qdrant.vector_size:
            raise ValueError("TEI query embedding 维度与 Qdrant 配置不一致")
        result = await self._client.query_points(
            collection_name=self._collection,
            query=vector,
            query_filter=Filter(must=_filter_conditions(source, kinds)),
            limit=limit,
            with_payload=["memory_uid"],
        )
        return [
            str(point.payload["memory_uid"])
            for point in result.points
            if point.payload and "memory_uid" in point.payload
        ]
