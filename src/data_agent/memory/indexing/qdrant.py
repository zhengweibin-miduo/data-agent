"""Qdrant 向量记忆投影。"""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from qdrant_client.async_qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Condition,
    Distance,
    FieldCondition,
    Filter,
    IsNullCondition,
    MatchAny,
    MatchValue,
    PayloadField,
    PayloadSchemaType,
    PointIdsList,
    PointStruct,
    VectorParams,
)

from data_agent.models.memory import MemoryProjection
from data_agent.settings import app_config


def _qdrant_point_id(memory_uid: str) -> str:
    """把 64 位内容 UID 映射为 Qdrant 支持的稳定 UUID。"""
    return str(uuid5(NAMESPACE_URL, f"data-agent-memory:{memory_uid}"))


def _filter_conditions(
    source: str,
    categories: set[str] | None,
    user_id: str | None,
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
    conditions.append(
        IsNullCondition(is_null=PayloadField(key="user_id"))
        if user_id is None
        else FieldCondition(key="user_id", match=MatchValue(value=user_id))
    )
    if categories:
        conditions.append(
            FieldCondition(
                key="category",
                match=MatchAny(any=sorted(categories)),
            )
        )
    return conditions


class MemoryQdrantIndex:
    """管理项目专用 Qdrant 记忆集合。"""

    def __init__(self, client: AsyncQdrantClient) -> None:
        """绑定共享异步客户端。"""
        self._client = client
        self._collection = app_config.qdrant.memory_collection

    async def setup(self) -> None:
        """幂等创建集合与过滤载荷索引。"""
        # 步骤一：集合不存在时按当前向量维度和距离度量创建。
        if not await self._client.collection_exists(self._collection):
            await self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(
                    size=app_config.qdrant.vector_size,
                    distance=Distance(app_config.qdrant.distance),
                ),
            )
        # 步骤二：幂等补齐严格作用域检索需要的载荷索引。
        for field in (
            "source",
            "user_id",
            "category",
            "status",
            "lifecycle_policy",
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
        """只重建项目配置的专用集合。"""
        # 步骤一：仅删除配置明确指定的项目专用集合。
        if await self._client.collection_exists(self._collection):
            await self._client.delete_collection(self._collection)
        # 步骤二：按当前向量与过滤配置重新创建集合。
        await self.setup()

    async def upsert(
        self,
        projection: MemoryProjection,
        vector: list[float],
    ) -> None:
        """校验向量维度后按稳定点 ID 幂等覆盖。"""
        # 步骤一：写入前校验向量维度与集合配置一致。
        if len(vector) != app_config.qdrant.vector_size:
            raise ValueError("TEI embedding 维度与 Qdrant 配置不一致")
        # 步骤二：以稳定点 ID 幂等覆盖向量和有界投影载荷。
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
        categories: set[str] | None,
        limit: int,
        *,
        user_id: str | None = None,
    ) -> list[str]:
        """在严格作用域过滤后执行向量检索。"""
        # 步骤一：先校验查询向量维度，避免错误向量进入远端检索。
        if len(vector) != app_config.qdrant.vector_size:
            raise ValueError("TEI query embedding 维度与 Qdrant 配置不一致")
        # 步骤二：在来源、租户、类别、状态和版本过滤后执行向量查询。
        result = await self._client.query_points(
            collection_name=self._collection,
            query=vector,
            query_filter=Filter(must=_filter_conditions(source, categories, user_id)),
            limit=limit,
            with_payload=["memory_uid"],
        )
        # 步骤三：只返回具备稳定记忆 UID 的候选，不信任派生载荷内容。
        return [
            str(point.payload["memory_uid"])
            for point in result.points
            if point.payload and "memory_uid" in point.payload
        ]
