"""元数据同步编排与转换。"""

from collections.abc import Sequence
from dataclasses import asdict
from hashlib import sha256
from json import dumps
from uuid import NAMESPACE_URL, UUID, uuid5

from loguru import logger
from qdrant_client.models import PointStruct

from app.client.tei_embedding_client_manager import TeiEmbeddings
from app.conf.meta_config import MetaConfig
from app.entity.column_info import ColumnInfo
from app.entity.column_metric import ColumnMetric
from app.entity.metric_info import MetricInfo
from app.entity.table_info import TableInfo
from app.entity.value_info import ValueInfo
from app.repository.metadata_repository import (
    BM25_VECTOR_NAME,
    EMBEDDING_DIMENSION,
    MetadataRepository,
    bm25_document,
)

COLUMN_COLLECTION = "data-agent-column"
METRIC_COLLECTION = "data-agent-metric"
EMBEDDING_BATCH_SIZE = 10
EXAMPLE_LIMIT = 10
SYNC_VALUE_LIMIT = 100_000

type VectorEntity = ColumnInfo | MetricInfo
type VectorText = tuple[VectorEntity, str, str]


def stable_point_id(
    collection_name: str,
    entity_id: str,
    text_kind: str,
    value: str,
) -> UUID:
    """为一个实体文本生成稳定 Qdrant UUID。"""
    identity = dumps(
        [collection_name, entity_id, text_kind, value],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return uuid5(NAMESPACE_URL, identity)


def stable_value_id(column_id: str, value: str) -> str:
    """为一个字段值生成稳定 Elasticsearch 文档 ID。"""
    return sha256(f"{column_id}\0{value}".encode()).hexdigest()


class MetadataSyncService:
    """校验并同步表、字段和指标元数据。"""

    def __init__(
        self,
        repository: MetadataRepository,
        embeddings: TeiEmbeddings,
    ) -> None:
        self._repository = repository
        self._embeddings = embeddings

    async def sync(self, config: MetaConfig) -> None:
        """执行一次可安全重放的元数据同步。"""
        schema = await self._repository.get_dw_schema()
        self._validate_dw_schema(config, schema)
        logger.info("DW 结构校验通过，表数量={}", len(config.tables))

        table_entities = [
            TableInfo(
                id=table.name,
                name=table.name,
                role=table.role,
                description=table.description,
            )
            for table in config.tables
        ]
        column_entities: list[ColumnInfo] = []

        for table in config.tables:
            for column in table.columns:
                column_id = f"{table.name}.{column.name}"
                examples = await self._repository.get_distinct_values(
                    table.name,
                    column.name,
                    EXAMPLE_LIMIT,
                )
                column_entities.append(
                    ColumnInfo(
                        id=column_id,
                        name=column.name,
                        type=schema[table.name][column.name],
                        role=column.role,
                        examples=examples,
                        description=column.description,
                        alias=column.alias,
                        table_id=table.name,
                    )
                )

        metric_entities = [
            MetricInfo(
                id=metric.name,
                name=metric.name,
                description=metric.description,
                relevant_columns=metric.relevant_columns,
                alias=metric.alias,
            )
            for metric in config.metrics
        ]
        relation_entities = [
            ColumnMetric(column_id=column_id, metric_id=metric.name)
            for metric in config.metrics
            for column_id in metric.relevant_columns
        ]

        await self._repository.upsert_metadata(
            table_entities,
            column_entities,
            metric_entities,
            relation_entities,
        )
        logger.info(
            "Meta MySQL upsert 完成，表={}，字段={}，指标={}，关系={}",
            len(table_entities),
            len(column_entities),
            len(metric_entities),
            len(relation_entities),
        )

        await self._sync_vectors(COLUMN_COLLECTION, column_entities)
        await self._sync_vectors(METRIC_COLLECTION, metric_entities)

        synchronized_values = 0
        for table in config.tables:
            for column in table.columns:
                if not column.sync:
                    continue
                column_id = f"{table.name}.{column.name}"
                values = await self._repository.get_distinct_values(
                    table.name,
                    column.name,
                    SYNC_VALUE_LIMIT,
                )
                documents = [
                    ValueInfo(
                        id=stable_value_id(column_id, value),
                        value=value,
                        column_id=column_id,
                    )
                    for value in values
                ]
                await self._repository.upsert_values(documents)
                synchronized_values += len(documents)
                logger.info(
                    "Elasticsearch 字段值 upsert 完成，字段={}，数量={}",
                    column_id,
                    len(documents),
                )

        logger.info("元数据同步完成，字段值总数={}", synchronized_values)

    async def _sync_vectors(
        self,
        collection_name: str,
        entities: Sequence[VectorEntity],
    ) -> None:
        texts: list[VectorText] = []
        for entity in entities:
            seen: set[str] = set()
            for text_kind, value in [
                ("name", entity.name),
                ("description", entity.description),
                *(("alias", alias) for alias in entity.alias),
            ]:
                if value in seen:
                    continue
                seen.add(value)
                texts.append((entity, text_kind, value))

        points: list[PointStruct] = []
        for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            batch = texts[start : start + EMBEDDING_BATCH_SIZE]
            vectors = await self._embeddings.aembed_documents(
                [value for _, _, value in batch]
            )
            if len(vectors) != len(batch):
                raise RuntimeError(
                    f"TEI 返回向量数量不匹配: 期望 {len(batch)}，实际 {len(vectors)}"
                )
            for (entity, text_kind, value), vector in zip(
                batch,
                vectors,
                strict=True,
            ):
                if len(vector) != EMBEDDING_DIMENSION:
                    raise RuntimeError(
                        f"TEI embedding 必须为 {EMBEDDING_DIMENSION} 维，"
                        f"实际为 {len(vector)} 维"
                    )
                points.append(
                    PointStruct(
                        id=stable_point_id(
                            collection_name,
                            entity.id,
                            text_kind,
                            value,
                        ),
                        vector={
                            "": vector,
                            BM25_VECTOR_NAME: bm25_document(value),
                        },
                        payload={
                            **asdict(entity),
                            "text_kind": text_kind,
                            "text": value,
                        },
                    )
                )

        await self._repository.upsert_vectors(collection_name, points)
        logger.info(
            "Qdrant upsert 完成，collection={}，点数量={}",
            collection_name,
            len(points),
        )

    @staticmethod
    def _validate_dw_schema(
        config: MetaConfig,
        schema: dict[str, dict[str, str]],
    ) -> None:
        for table in config.tables:
            actual_columns = schema.get(table.name)
            if actual_columns is None:
                raise ValueError(f"DW 表不存在: {table.name}")
            missing = {
                column.name
                for column in table.columns
                if column.name not in actual_columns
            }
            if missing:
                raise ValueError(
                    f"DW 表 {table.name} 缺少字段: {', '.join(sorted(missing))}"
                )
