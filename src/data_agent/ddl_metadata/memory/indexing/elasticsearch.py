"""Elasticsearch BM25 记忆投影。"""

from elasticsearch import AsyncElasticsearch, NotFoundError

from data_agent.ddl_metadata.models.memory import MemoryProjection
from data_agent.settings import app_config


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
        categories: set[str] | None,
        limit: int,
        *,
        user_id: str | None = None,
    ) -> list[str]:
        """在严格作用域过滤后执行 BM25 检索。"""
        filters: list[dict[str, object]] = [
            {"term": {"source": source}},
            {"term": {"status": "ACTIVE"}},
            {"term": {"content_version": app_config.memory.content_version}},
            {"term": {"projection_version": app_config.memory.projection_version}},
        ]
        filters.append(
            {"bool": {"must_not": {"exists": {"field": "user_id"}}}}
            if user_id is None
            else {"term": {"user_id": user_id}}
        )
        if categories:
            filters.append({"terms": {"category": sorted(categories)}})
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
