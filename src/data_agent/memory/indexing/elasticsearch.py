"""Elasticsearch BM25 记忆投影。"""

from typing import Any, cast

from elasticsearch import AsyncElasticsearch, NotFoundError

from data_agent.errors import DataAgentError
from data_agent.models.memory import MemoryProjection
from data_agent.settings import app_config

_ANALYZER_NAME = "memory_zh"


def _nested(payload: dict[str, Any], *keys: str) -> object | None:
    """按键路径逐层读取嵌套响应，任一层缺失即返回 None。"""
    # 步骤一：逐层下钻，遇到非字典或缺键立即结束，避免链式 get 的类型噪音。
    current: object | None = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


class MemoryElasticsearchIndex:
    """管理项目专用 BM25 记忆索引。"""

    def __init__(self, client: AsyncElasticsearch) -> None:
        """绑定共享异步客户端。"""
        self._client = client
        self._index = app_config.elasticsearch.memory_index

    async def setup(self) -> None:
        """幂等创建当前投影版本的映射，并拒绝复用被动态映射污染的索引。"""
        # 步骤一：已存在的项目专用索引先复核映射再复用，避免破坏当前投影。
        # recreate 的 delete 与 create 之间若有写入落地，Elasticsearch 会按
        # auto_create_index 用动态默认映射自动建索引；只检查存在性会让严格映射与
        # 中文分析器静默丢失——中文 BM25 质量下降却没有任何报错。
        if await self._client.indices.exists(index=self._index):
            await self._verify_mapping()
            return
        # 步骤二：不存在时创建严格映射，拒绝未声明的投影字段。
        await self._client.indices.create(
            index=self._index,
            settings={
                "analysis": {
                    "analyzer": {
                        _ANALYZER_NAME: {
                            "type": "custom",
                            "tokenizer": app_config.elasticsearch.analyzer,
                        }
                    }
                }
            },
            mappings={
                "dynamic": "strict",
                "properties": {
                    "memory_text": {"type": "text", "analyzer": _ANALYZER_NAME},
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

    async def _verify_mapping(self) -> None:
        """复核既有索引与当前配置声明的严格映射、分析器绑定完全一致。

        只检查"存在一个同名分析器"是不够的：索引可能建于旧配置，`memory_zh` 的
        tokenizer 仍是上一版取值；或 `memory_text` 根本没有绑定该分析器。两种情况下
        中文 BM25 会持续按错误分词检索，结果显著劣化却没有任何报错。因此逐项核对
        `dynamic`、`memory_text` 的类型与 analyzer 绑定、以及分析器的 tokenizer。

        Raises:
            DataAgentError: 既有索引的映射或分析器绑定与当前配置不一致。
        """
        # 步骤一：读取既有索引的映射与分析设置，按响应体逐层取值。
        mapping = cast(
            dict[str, Any],
            (await self._client.indices.get_mapping(index=self._index)).body,
        )
        settings = cast(
            dict[str, Any],
            (await self._client.indices.get_settings(index=self._index)).body,
        )
        dynamic = _nested(mapping, self._index, "mappings", "dynamic")
        memory_text = _nested(
            mapping,
            self._index,
            "mappings",
            "properties",
            "memory_text",
        )
        analyzer = _nested(
            settings,
            self._index,
            "settings",
            "index",
            "analysis",
            "analyzer",
            _ANALYZER_NAME,
        )
        # 步骤二：逐项判定并记录首个不一致原因，使报错能直接定位到失配的部分。
        expected_tokenizer = app_config.elasticsearch.analyzer
        reason: str | None = None
        if str(dynamic).casefold() != "strict":
            reason = "缺少严格映射，索引可能由动态映射自动创建"
        elif not isinstance(memory_text, dict):
            reason = "缺少 memory_text 字段映射"
        elif str(memory_text.get("type")) != "text":
            reason = "memory_text 不是全文检索字段"
        elif str(memory_text.get("analyzer")) != _ANALYZER_NAME:
            reason = "memory_text 未绑定当前中文分析器"
        elif not isinstance(analyzer, dict):
            reason = "缺少中文分析器定义"
        elif str(analyzer.get("tokenizer")) != expected_tokenizer:
            reason = "中文分析器的分词器与当前配置不一致"
        # 步骤三：任一项不一致都必须显式失败；继续复用会静默降级中文检索质量。
        if reason is not None:
            raise DataAgentError(
                "memory_index_mapping_invalid",
                "memory_index_setup",
                f"既有记忆索引与当前配置不一致：{reason}",
                http_status=500,
                details={
                    "index": self._index,
                    "analyzer": _ANALYZER_NAME,
                    "tokenizer": expected_tokenizer,
                    "reason": reason,
                },
            )

    async def recreate(self) -> None:
        """只重建项目配置的专用索引。"""
        # 步骤一：仅删除配置明确指定的项目专用索引。
        if await self._client.indices.exists(index=self._index):
            await self._client.indices.delete(index=self._index)
        # 步骤二：按当前投影版本重新创建严格映射。删除与创建之间若有并发写入按
        # 动态映射建出索引，setup 的映射复核会失败而不是静默复用降级索引。
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
