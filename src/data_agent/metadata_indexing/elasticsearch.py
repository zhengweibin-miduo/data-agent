"""Elasticsearch Meta 字段值投影。"""

import hashlib
from typing import Any, cast

from elasticsearch import AsyncElasticsearch

from data_agent.errors import DataAgentError
from data_agent.metadata_indexing.models import MetadataValueProjection
from data_agent.settings import app_config

_ANALYZER = "metadata_value_zh"


def metadata_value_document_id(column_id: str, value: str) -> str:
    """生成字段内规范值的稳定文档 ID。"""
    return hashlib.sha256(f"{column_id}\0{value}".encode()).hexdigest()


class MetadataValueElasticsearchIndex:
    """管理项目专用字段值索引。"""

    def __init__(self, client: AsyncElasticsearch) -> None:
        """绑定共享异步 Elasticsearch 客户端。"""
        self._client = client
        self._index = app_config.elasticsearch.metadata_value_index

    def _mappings(self) -> dict[str, object]:
        """返回唯一严格 mapping 声明。"""
        return {
            "dynamic": "strict",
            "properties": {
                "column_id": {"type": "keyword"},
                "table_id": {"type": "keyword"},
                "value_text": {"type": "text", "analyzer": _ANALYZER},
                "value_keyword": {"type": "keyword", "ignore_above": 8191},
                "frequency": {"type": "long"},
                "refresh_version": {"type": "keyword"},
                "schema_fingerprint": {"type": "keyword"},
            },
        }

    async def setup(self) -> None:
        """创建或严格校验字段值索引。"""
        if not await self._client.indices.exists(index=self._index):
            await self._client.indices.create(
                index=self._index,
                settings={
                    "analysis": {
                        "analyzer": {
                            _ANALYZER: {
                                "type": "custom",
                                "tokenizer": app_config.elasticsearch.analyzer,
                            }
                        }
                    }
                },
                mappings=self._mappings(),
            )
            return
        mapping = cast(
            dict[str, Any],
            (await self._client.indices.get_mapping(index=self._index)).body,
        )
        settings = cast(
            dict[str, Any],
            (await self._client.indices.get_settings(index=self._index)).body,
        )
        actual = mapping.get(self._index, {}).get("mappings", {})
        analyzer = (
            settings.get(self._index, {})
            .get("settings", {})
            .get("index", {})
            .get("analysis", {})
            .get("analyzer", {})
            .get(_ANALYZER, {})
        )
        properties = actual.get("properties", {})
        expected_properties = cast(dict[str, Any], self._mappings()["properties"])
        if (
            str(actual.get("dynamic")).casefold() != "strict"
            or properties != expected_properties
            or analyzer.get("tokenizer") != app_config.elasticsearch.analyzer
        ):
            raise DataAgentError(
                "metadata_value_mapping_invalid",
                "metadata_index_setup",
                "既有 Meta 字段值索引与当前严格 mapping 不一致",
                details={"index": self._index},
            )

    async def recreate(self) -> None:
        """仅重建配置明确指定的字段值索引。"""
        if await self._client.indices.exists(index=self._index):
            await self._client.indices.delete(index=self._index)
        await self.setup()

    async def refresh_table(
        self,
        table_id: str,
        refresh_version: str,
        projections: list[MetadataValueProjection],
    ) -> None:
        """批量覆盖当前 top-N，并清理表内旧刷新版本。"""
        if projections:
            operations: list[dict[str, object]] = []
            for projection in projections:
                operations.extend(
                    [
                        {
                            "index": {
                                "_index": self._index,
                                "_id": metadata_value_document_id(
                                    projection.column_id, projection.value_keyword
                                ),
                            }
                        },
                        projection.model_dump(mode="json"),
                    ]
                )
            response = await self._client.bulk(operations=operations, refresh=False)
            if response.get("errors"):
                raise RuntimeError("Elasticsearch Meta 字段值 bulk 写入失败")
        await self._client.delete_by_query(
            index=self._index,
            conflicts="proceed",
            refresh=False,
            query={
                "bool": {
                    "filter": [{"term": {"table_id": table_id}}],
                    "must_not": [{"term": {"refresh_version": refresh_version}}],
                }
            },
        )

    async def search(
        self,
        query: str,
        column_ids: set[str],
        limit: int,
    ) -> list[MetadataValueProjection]:
        """只在明确候选字段范围内组合精确与模糊匹配。"""
        if not column_ids:
            raise ValueError("字段值检索必须限定候选 column_ids")
        response = await self._client.search(
            index=self._index,
            size=limit,
            query={
                "bool": {
                    "filter": [{"terms": {"column_id": sorted(column_ids)}}],
                    "should": [
                        {"term": {"value_keyword": {"value": query, "boost": 4}}},
                        {"match": {"value_text": {"query": query}}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        )
        return [
            MetadataValueProjection.model_validate(hit["_source"])
            for hit in response["hits"]["hits"]
        ]
