"""Elasticsearch Meta 字段值投影。"""

import hashlib
import json
from collections.abc import (
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Iterator,
    Sequence,
)
from typing import Any, cast

from elasticsearch import AsyncElasticsearch

from data_agent.errors import DataAgentError
from data_agent.metadata_indexing.models import MetadataValueProjection
from data_agent.settings import app_config

_ANALYZER = "metadata_value_zh"
_BULK_DOCUMENT_LIMIT = 500
_BULK_BYTE_LIMIT = 5 * 1024 * 1024
_REFRESH_VERSION_PAGE_SIZE = 500


def _analyzer_config(tokenizer: str) -> dict[str, object]:
    """返回字段值索引声明的完整 analyzer 配置。"""
    return {
        "type": "custom",
        "tokenizer": tokenizer,
        "filter": [],
        "char_filter": [],
    }


def _normalized_analyzer(config: dict[str, Any]) -> dict[str, object]:
    """规范化 Elasticsearch 省略的空 analyzer 列表。"""
    return {
        "type": config.get("type"),
        "tokenizer": config.get("tokenizer"),
        "filter": config.get("filter", []),
        "char_filter": config.get("char_filter", []),
    }


def metadata_value_document_id(
    table_id: str,
    column_id: str,
    value_hash: str,
) -> str:
    """生成表、字段与规范值身份共同决定的稳定文档 ID。"""
    return hashlib.sha256(
        f"{table_id}\0{column_id}\0{value_hash}".encode()
    ).hexdigest()


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
                            _ANALYZER: _analyzer_config(
                                app_config.elasticsearch.analyzer
                            )
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
            or _normalized_analyzer(analyzer)
            != _analyzer_config(app_config.elasticsearch.analyzer)
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

    async def upsert_projections(
        self,
        projections: AsyncIterable[MetadataValueProjection],
        heartbeat: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """按既有双预算幂等写入一批字段值投影。"""
        async for operations in _async_bulk_chunks(projections):
            if heartbeat is not None:
                await heartbeat()
            response = await self._client.bulk(operations=operations, refresh=False)
            if response.get("errors"):
                raise RuntimeError("Elasticsearch Meta 字段值 bulk 写入失败")

    async def upsert_batch(
        self,
        projections: list[MetadataValueProjection],
    ) -> None:
        """写入一个已由状态机双预算限制的 UPSERT 批次。"""
        chunks = list(_bulk_chunks(projections))
        if len(chunks) > 1:
            raise ValueError("字段值发布工作单元超过 Elasticsearch bulk 字节预算")
        if not chunks:
            return
        response = await self._client.bulk(operations=chunks[0], refresh=False)
        if response.get("errors"):
            raise RuntimeError("Elasticsearch Meta 字段值 bulk 写入失败")

    async def delete_documents(self, document_ids: Sequence[str]) -> None:
        """按明确稳定 ID 有界删除字段值文档。"""
        operations = [
            {"delete": {"_index": self._index, "_id": document_id}}
            for document_id in document_ids
        ]
        if not operations:
            return
        response = await self._client.bulk(operations=operations, refresh=False)
        if response.get("errors"):
            failures = [
                item
                for item in response.get("items", [])
                if int(item.get("delete", {}).get("status", 500)) not in {200, 404}
            ]
            if failures:
                raise RuntimeError("Elasticsearch Meta 字段值 bulk 删除失败")

    async def refresh(self) -> None:
        """在 COMPLETE 前刷新当前值索引可见性。"""
        await self._client.indices.refresh(index=self._index)

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
                        {
                            "match": {
                                "value_text": {
                                    "query": query,
                                    "fuzziness": "AUTO",
                                    "max_expansions": 50,
                                    "prefix_length": 1,
                                }
                            }
                        },
                    ],
                    "minimum_should_match": 1,
                }
            },
        )
        return [
            MetadataValueProjection.model_validate(hit["_source"])
            for hit in response["hits"]["hits"]
        ]

    async def current_refresh_versions(
        self, table_ids: set[str]
    ) -> dict[str, frozenset[str]]:
        """分页读取每张表当前可见的完整刷新代次集合。"""
        if not table_ids:
            return {}
        collected: dict[str, set[str]] = {}
        after: dict[str, object] | None = None
        while True:
            composite: dict[str, object] = {
                "size": _REFRESH_VERSION_PAGE_SIZE,
                "sources": [
                    {"table_id": {"terms": {"field": "table_id"}}},
                    {"refresh_version": {"terms": {"field": "refresh_version"}}},
                ],
            }
            if after is not None:
                composite["after"] = after
            response = await self._client.search(
                index=self._index,
                size=0,
                query={"terms": {"table_id": sorted(table_ids)}},
                aggs={"versions": {"composite": composite}},
            )
            aggregation = response["aggregations"]["versions"]
            for bucket in aggregation["buckets"]:
                key = bucket["key"]
                collected.setdefault(str(key["table_id"]), set()).add(
                    str(key["refresh_version"])
                )
            next_after = aggregation.get("after_key")
            if not next_after:
                break
            after = cast(dict[str, object], next_after)
        return {table_id: frozenset(values) for table_id, values in collected.items()}


def _bulk_chunks(
    projections: Iterable[MetadataValueProjection],
) -> Iterator[list[dict[str, object]]]:
    """按文档数和 NDJSON 字节双预算惰性生成 bulk 分块。"""
    current: list[dict[str, object]] = []
    current_bytes = 0
    for projection in projections:
        pair, pair_bytes = _bulk_pair(projection)
        if pair_bytes > _BULK_BYTE_LIMIT:
            raise ValueError("单个 Meta 字段值超过 Elasticsearch bulk 字节上限")
        if current and (
            len(current) // 2 >= _BULK_DOCUMENT_LIMIT
            or current_bytes + pair_bytes > _BULK_BYTE_LIMIT
        ):
            yield current
            current = []
            current_bytes = 0
        current.extend(pair)
        current_bytes += pair_bytes
    if current:
        yield current


async def _async_bulk_chunks(
    projections: AsyncIterable[MetadataValueProjection],
) -> AsyncIterator[list[dict[str, object]]]:
    """消费异步投影流，并在达到预算时立即产出 bulk 分块。"""
    current: list[dict[str, object]] = []
    current_bytes = 0
    async for projection in projections:
        pair, pair_bytes = _bulk_pair(projection)
        if current and (
            len(current) // 2 >= _BULK_DOCUMENT_LIMIT
            or current_bytes + pair_bytes > _BULK_BYTE_LIMIT
        ):
            yield current
            current = []
            current_bytes = 0
        current.extend(pair)
        current_bytes += pair_bytes
    if current:
        yield current


def _bulk_pair(
    projection: MetadataValueProjection,
) -> tuple[list[dict[str, object]], int]:
    """构造单文档 bulk 操作并计算其 NDJSON 字节数。"""
    pair: list[dict[str, object]] = [
        {
            "index": {
                "_index": app_config.elasticsearch.metadata_value_index,
                "_id": metadata_value_document_id(
                    projection.table_id,
                    projection.column_id,
                    projection.value_hash,
                ),
            }
        },
        projection.model_dump(mode="json"),
    ]
    pair_bytes = sum(
        len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode()) + 1
        for item in pair
    )
    if pair_bytes > _BULK_BYTE_LIMIT:
        raise ValueError("单个 Meta 字段值超过 Elasticsearch bulk 字节上限")
    return pair, pair_bytes
