"""元数据同步的数据读写。"""

import re
from collections.abc import Sequence
from dataclasses import asdict
from itertools import groupby

from elasticsearch import AsyncElasticsearch
from elasticsearch.helpers import async_bulk
from qdrant_client.async_qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Bm25Config,
    Distance,
    Document,
    Modifier,
    PointStruct,
    SparseVectorConfig,
    SparseVectorNameConfig,
    SparseVectorParams,
    TokenizerType,
    VectorParams,
)
from sqlalchemy import text
from sqlalchemy.dialects.mysql import Insert, insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.entity.column_info import ColumnInfo
from app.entity.column_metric import ColumnMetric
from app.entity.metric_info import MetricInfo
from app.entity.table_info import TableInfo
from app.entity.value_info import ValueInfo
from app.model.column_info import ColumnInfoMySQL
from app.model.column_metric import ColumnMetricMySQL
from app.model.metric_info import MetricInfoMySQL
from app.model.table_info import TableInfoMySQL

EMBEDDING_DIMENSION = 512
BM25_VECTOR_NAME = "bm25"
BM25_MODEL = "Qdrant/bm25"
BM25_CONFIG = Bm25Config(
    k=1.2,
    b=0.75,
    avg_len=256,
    tokenizer=TokenizerType.MULTILINGUAL,
    language="none",
    lowercase=True,
)
VALUE_INDEX = "data-agent-value"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

type MetadataEntity = TableInfo | ColumnInfo | MetricInfo | ColumnMetric

_TABLE_INSERT = mysql_insert(TableInfoMySQL)
_TABLE_UPSERT = _TABLE_INSERT.on_duplicate_key_update(
    name=_TABLE_INSERT.inserted.name,
    role=_TABLE_INSERT.inserted.role,
    description=_TABLE_INSERT.inserted.description,
)
_COLUMN_INSERT = mysql_insert(ColumnInfoMySQL)
_COLUMN_UPSERT = _COLUMN_INSERT.on_duplicate_key_update(
    name=_COLUMN_INSERT.inserted.name,
    type=_COLUMN_INSERT.inserted.type,
    role=_COLUMN_INSERT.inserted.role,
    examples=_COLUMN_INSERT.inserted.examples,
    description=_COLUMN_INSERT.inserted.description,
    alias=_COLUMN_INSERT.inserted.alias,
    table_id=_COLUMN_INSERT.inserted.table_id,
)
_METRIC_INSERT = mysql_insert(MetricInfoMySQL)
_METRIC_UPSERT = _METRIC_INSERT.on_duplicate_key_update(
    name=_METRIC_INSERT.inserted.name,
    description=_METRIC_INSERT.inserted.description,
    relevant_columns=_METRIC_INSERT.inserted.relevant_columns,
    alias=_METRIC_INSERT.inserted.alias,
)
_RELATION_INSERT = mysql_insert(ColumnMetricMySQL)
_RELATION_UPSERT = _RELATION_INSERT.on_duplicate_key_update(
    metric_id=_RELATION_INSERT.inserted.metric_id,
)


def bm25_document(text: str) -> Document:
    """使用同步与查询共享的固定配置构造 BM25 文档。"""
    return Document(text=text, model=BM25_MODEL, options=BM25_CONFIG)


class MetadataRepository:
    """封装一次元数据同步所需的三种存储操作。"""

    def __init__(
        self,
        session: AsyncSession,
        qdrant: AsyncQdrantClient,
        elasticsearch: AsyncElasticsearch,
    ) -> None:
        self._session = session
        self._qdrant = qdrant
        self._elasticsearch = elasticsearch

    async def get_dw_schema(self) -> dict[str, dict[str, str]]:
        """读取 DW 的真实表、字段和字段类型。"""
        rows = (
            await self._session.execute(
                text(
                    """
                    SELECT TABLE_NAME AS table_name,
                           COLUMN_NAME AS column_name,
                           COLUMN_TYPE AS column_type
                    FROM information_schema.columns
                    WHERE TABLE_SCHEMA = :schema
                    ORDER BY TABLE_NAME, ORDINAL_POSITION
                    """
                ),
                {"schema": "dw"},
            )
        ).mappings()
        return {
            table_name: {
                str(row["column_name"]): str(row["column_type"])
                for row in table_rows
            }
            for table_name, table_rows in groupby(
                rows,
                key=lambda row: str(row["table_name"]),
            )
        }

    async def get_distinct_values(
        self,
        table_name: str,
        column_name: str,
        limit: int,
    ) -> list[str]:
        """读取受控 DW 字段的去重非空字符串值。"""
        if not _IDENTIFIER.fullmatch(table_name) or not _IDENTIFIER.fullmatch(
            column_name
        ):
            raise ValueError("DW 表名和字段名必须是简单 MySQL 标识符")
        if limit < 1:
            raise ValueError("字段取值上限必须大于 0")

        result = await self._session.execute(
            text(
                f"SELECT DISTINCT `{column_name}` FROM `dw`.`{table_name}` "
                f"WHERE `{column_name}` IS NOT NULL LIMIT :limit"
            ),
            {"limit": limit},
        )
        return [str(value) for value in result.scalars()]

    async def upsert_metadata(
        self,
        tables: Sequence[TableInfo],
        columns: Sequence[ColumnInfo],
        metrics: Sequence[MetricInfo],
        relations: Sequence[ColumnMetric],
    ) -> None:
        """批量幂等写入四张 Meta 表。"""
        await self._execute_many(_TABLE_UPSERT, tables)
        await self._execute_many(_COLUMN_UPSERT, columns)
        await self._execute_many(_METRIC_UPSERT, metrics)
        await self._execute_many(_RELATION_UPSERT, relations)

    async def upsert_vectors(
        self,
        collection_name: str,
        points: Sequence[PointStruct],
    ) -> None:
        """创建或校验 collection 后幂等写入向量点。"""
        for point in points:
            vector = point.vector
            if not isinstance(vector, dict):
                raise RuntimeError(
                    f"Qdrant collection {collection_name} 的点必须同时包含"
                    "匿名 dense 和命名 BM25 向量"
                )
            unexpected_vectors = set(vector) - {"", BM25_VECTOR_NAME}
            if unexpected_vectors:
                raise RuntimeError(
                    f"Qdrant collection {collection_name} 的新点不得写入其他向量: "
                    f"{', '.join(sorted(unexpected_vectors))}"
                )
            dense = vector.get("")
            if not isinstance(dense, list) or len(dense) != EMBEDDING_DIMENSION:
                raise RuntimeError(
                    f"Qdrant collection {collection_name} 的匿名 dense 向量必须为 "
                    f"{EMBEDDING_DIMENSION} 维"
                )
            document = vector.get(BM25_VECTOR_NAME)
            if not isinstance(document, Document):
                raise RuntimeError(
                    f"Qdrant collection {collection_name} 缺少命名 "
                    f"{BM25_VECTOR_NAME} BM25 Document"
                )
            if (
                not document.text.strip()
                or document.model != BM25_MODEL
                or not isinstance(document.options, Bm25Config)
                or document.options != BM25_CONFIG
            ):
                raise RuntimeError(
                    f"Qdrant collection {collection_name} 的 "
                    f"{BM25_VECTOR_NAME} Document 必须使用固定 Qdrant/bm25 配置"
                )
        await self._ensure_collection(collection_name)
        if points:
            await self._qdrant.upsert(
                collection_name=collection_name,
                points=points,
                wait=True,
            )

    async def upsert_values(
        self,
        documents: Sequence[ValueInfo],
    ) -> None:
        """创建字段值索引并通过稳定文档 ID 幂等写入。"""
        if not await self._elasticsearch.indices.exists(index=VALUE_INDEX):
            await self._elasticsearch.indices.create(
                index=VALUE_INDEX,
                mappings={
                    "properties": {
                        "id": {"type": "keyword"},
                        "value": {
                            "type": "text",
                            "analyzer": "ik_max_word",
                            "search_analyzer": "ik_max_word",
                        },
                        "column_id": {"type": "keyword"},
                    }
                },
            )
        if not documents:
            return

        await async_bulk(
            self._elasticsearch,
            (
                {
                    "_op_type": "index",
                    "_index": VALUE_INDEX,
                    "_id": document.id,
                    "_source": asdict(document),
                }
                for document in documents
            ),
            refresh="wait_for",
        )

    async def _execute_many(
        self,
        statement: Insert,
        entities: Sequence[MetadataEntity],
    ) -> None:
        if entities:
            await self._session.execute(
                statement,
                [asdict(entity) for entity in entities],
            )

    async def _ensure_collection(self, collection_name: str) -> None:
        if not await self._qdrant.collection_exists(collection_name=collection_name):
            await self._qdrant.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(
                    size=EMBEDDING_DIMENSION,
                    distance=Distance.COSINE,
                ),
                sparse_vectors_config={
                    BM25_VECTOR_NAME: SparseVectorParams(modifier=Modifier.IDF)
                },
            )
            return

        info = await self._qdrant.get_collection(collection_name=collection_name)
        vectors = info.config.params.vectors
        if (
            not isinstance(vectors, VectorParams)
            or vectors.size != EMBEDDING_DIMENSION
            or vectors.distance != Distance.COSINE
        ):
            raise RuntimeError(
                f"Qdrant collection {collection_name} 不是匿名 "
                f"{EMBEDDING_DIMENSION} 维 Cosine 向量配置: {vectors!r}"
            )

        sparse_vectors = info.config.params.sparse_vectors
        if not sparse_vectors or BM25_VECTOR_NAME not in sparse_vectors:
            await self._qdrant.create_vector_name(
                collection_name=collection_name,
                vector_name=BM25_VECTOR_NAME,
                vector_name_config=SparseVectorNameConfig(
                    sparse=SparseVectorConfig(modifier=Modifier.IDF)
                ),
                wait=True,
            )
            return

        sparse = sparse_vectors[BM25_VECTOR_NAME]
        if not isinstance(sparse, SparseVectorParams) or sparse.modifier != Modifier.IDF:
            raise RuntimeError(
                f"Qdrant collection {collection_name} 的 {BM25_VECTOR_NAME} "
                f"不是 IDF 稀疏向量配置: {sparse!r}"
            )
