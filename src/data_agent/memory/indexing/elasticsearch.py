"""Elasticsearch BM25 记忆投影。"""

from elasticsearch import AsyncElasticsearch, NotFoundError

from data_agent.models.memory import MemoryProjection
from data_agent.settings import app_config


class MemoryElasticsearchIndex:
    """管理项目专用 BM25 记忆索引。"""

    def __init__(self, client: AsyncElasticsearch) -> None:
        """绑定共享异步客户端。"""
        self._client = client
        self._index = app_config.elasticsearch.memory_index

    async def setup(self) -> None:
        """幂等创建当前投影版本的映射。"""
        # 步骤一：已存在的项目专用索引直接复用，避免破坏当前投影。
        if await self._client.indices.exists(index=self._index):
            return
        # 步骤二：不存在时创建严格映射，拒绝未声明的投影字段。
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
                            "user_id",
                            "created_conversation_uid",
                            "created_message_uid",
                            "category",
                            "memory_key",
                            "content_schema",
                            "schema_fingerprint",
                            "object_ids",
                            "trust",
                            "status",
                            "lifecycle_policy",
                            "content_hash",
                            "content_version",
                            "projection_version",
                        )
                    },
                    "created_at": {"type": "date"},
                    "updated_at": {"type": "date"},
                    "expires_at": {"type": "date"},
                    "importance_score": {"type": "float"},
                    "record_version": {"type": "integer"},
                },
            },
        )

    async def recreate(self) -> None:
        """只重建项目配置的专用索引。"""
        # 步骤一：仅删除配置明确指定的项目专用索引。
        if await self._client.indices.exists(index=self._index):
            await self._client.indices.delete(index=self._index)
        # 步骤二：按当前投影版本重新创建严格映射。
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
        # 步骤一：按稳定 UID 删除项目索引中的目标文档。
        try:
            await self._client.delete(
                index=self._index,
                id=memory_uid,
                refresh=False,
            )
        # 步骤二：目标不存在等同于已达到删除期望状态。
        except NotFoundError:
            return

    async def search(
        self,
        query: str,
        source: str,
        categories: set[str] | None,
        limit: int,
        *,
        user_id: str | None = None,
    ) -> list[str]:
        """在严格作用域过滤后执行 BM25 检索。"""
        # 步骤一：先固定来源、活动状态和内容/投影版本边界。
        filters: list[dict[str, object]] = [
            {"term": {"source": source}},
            {"term": {"status": "ACTIVE"}},
            {"term": {"content_version": app_config.memory.content_version}},
            {"term": {"projection_version": app_config.memory.projection_version}},
        ]
        # 步骤二：追加租户与可选类别过滤，索引层不允许产生跨租户候选。
        filters.append(
            {"bool": {"must_not": {"exists": {"field": "user_id"}}}}
            if user_id is None
            else {"term": {"user_id": user_id}}
        )
        if categories:
            filters.append({"terms": {"category": sorted(categories)}})
        # 步骤三：在严格过滤后执行 BM25，并只返回候选 UID。
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
