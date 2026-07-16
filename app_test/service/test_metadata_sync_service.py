"""元数据同步配置、转换与存储边界检查。"""

import asyncio
import subprocess
import sys
from copy import deepcopy
from dataclasses import fields, is_dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch
from uuid import UUID

from elasticsearch import AsyncElasticsearch
from pydantic import ValidationError
from qdrant_client.async_qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Bm25Config,
    Distance,
    Document,
    Modifier,
    PointStruct,
    SparseVectorNameConfig,
    SparseVectorParams,
    TokenizerType,
    VectorParams,
)
from sqlalchemy import JSON, String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession

from app.client.tei_embedding_client_manager import TeiEmbeddings
from app.conf.meta_config import MetaConfig
from app.entity.column_info import ColumnInfo
from app.entity.column_metric import ColumnMetric
from app.entity.metric_info import MetricInfo
from app.entity.table_info import TableInfo
from app.entity.value_info import ValueInfo
from app.model.column_info import ColumnInfoMySQL
from app.model.column_metric import ColumnMetricMySQL
from app.model.metric_info import MetricInfoMySQL
from app.model.table_info import TableInfoMySQL
from app.repository.metadata_repository import (
    BM25_CONFIG,
    BM25_MODEL,
    BM25_VECTOR_NAME,
    MetadataRepository,
    bm25_document,
)
from app.script.sync_metadata import sync_metadata
from app.service.metadata_sync_service import (
    COLUMN_COLLECTION,
    METRIC_COLLECTION,
    MetadataSyncService,
    stable_point_id,
    stable_value_id,
)

SAMPLE_CONFIG = Path(__file__).parents[2] / "conf" / "meta_config.yaml"


def _config_payload() -> dict[str, Any]:
    return {
        "tables": [
            {
                "name": "dim_region",
                "role": "dim",
                "description": "地区维度表",
                "columns": [
                    {
                        "name": "region_id",
                        "role": "primary_key",
                        "description": "地区唯一标识",
                        "alias": ["地区ID"],
                        "sync": False,
                    },
                    {
                        "name": "province",
                        "role": "dimension",
                        "description": "省份名称",
                        "alias": ["省份", "省"],
                        "sync": True,
                    },
                ],
            }
        ],
        "metrics": [
            {
                "name": "GMV",
                "description": "成交金额总和",
                "relevant_columns": ["dim_region.province"],
                "alias": ["成交总额"],
            }
        ],
    }


def _assert_invalid(payload: dict[str, Any]) -> None:
    try:
        MetaConfig.model_validate(payload)
    except ValidationError:
        return
    raise AssertionError("非法元数据配置不应通过校验")


def _test_config() -> None:
    config = MetaConfig.from_yaml(SAMPLE_CONFIG)
    assert len(config.tables) == 5
    assert sum(len(table.columns) for table in config.tables) == 24
    assert len(config.metrics) == 2
    assert next(metric for metric in config.metrics if metric.name == "AOV").relevant_columns == [
        "fact_order.order_amount"
    ]

    payload = _config_payload()
    payload["tables"][0]["unknown"] = True
    _assert_invalid(payload)

    payload = _config_payload()
    payload["tables"].append(deepcopy(payload["tables"][0]))
    _assert_invalid(payload)

    payload = _config_payload()
    payload["tables"][0]["columns"].append(
        deepcopy(payload["tables"][0]["columns"][0])
    )
    _assert_invalid(payload)

    payload = _config_payload()
    payload["tables"][0]["name"] = "bad;drop"
    _assert_invalid(payload)

    payload = _config_payload()
    payload["metrics"].append(deepcopy(payload["metrics"][0]))
    _assert_invalid(payload)

    payload = _config_payload()
    payload["metrics"][0]["relevant_columns"].append("dim_region.province")
    _assert_invalid(payload)

    payload = _config_payload()
    payload["metrics"][0]["relevant_columns"] = ["dim_region.missing"]
    _assert_invalid(payload)

    with TemporaryDirectory() as directory:
        invalid_config = Path(directory) / "invalid.yaml"
        invalid_config.write_text("tables: [", encoding="utf-8")
        for config_path in [Path(directory) / "missing.yaml", invalid_config]:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "app.script.sync_metadata",
                    "--config",
                    str(config_path),
                ],
                cwd=SAMPLE_CONFIG.parents[1],
                capture_output=True,
                check=False,
            )
            assert result.returncode != 0


def _test_models() -> None:
    expected_columns: dict[
        type[Any],
        dict[str, tuple[type[Any], int | None, bool, bool]],
    ] = {
        TableInfoMySQL: {
            "id": (String, 64, False, True),
            "name": (String, 128, True, False),
            "role": (String, 32, True, False),
            "description": (Text, None, True, False),
        },
        ColumnInfoMySQL: {
            "id": (String, 64, False, True),
            "name": (String, 128, True, False),
            "type": (String, 64, True, False),
            "role": (String, 32, True, False),
            "examples": (JSON, None, True, False),
            "description": (Text, None, True, False),
            "alias": (JSON, None, True, False),
            "table_id": (String, 64, True, False),
        },
        MetricInfoMySQL: {
            "id": (String, 64, False, True),
            "name": (String, 128, True, False),
            "description": (Text, None, True, False),
            "relevant_columns": (JSON, None, True, False),
            "alias": (JSON, None, True, False),
        },
        ColumnMetricMySQL: {
            "column_id": (String, 64, False, True),
            "metric_id": (String, 64, False, True),
        },
    }

    for model, expected in expected_columns.items():
        table = model.__table__
        assert table.schema == "meta"
        assert list(table.columns.keys()) == list(expected)
        for name, (column_type, length, nullable, primary_key) in expected.items():
            column = table.c[name]
            assert isinstance(column.type, column_type)
            if length is not None:
                assert column.type.length == length
            assert column.nullable is nullable
            assert column.primary_key is primary_key


def _test_entities() -> None:
    expected_fields = {
        TableInfo: ("id", "name", "role", "description"),
        ColumnInfo: (
            "id",
            "name",
            "type",
            "role",
            "examples",
            "description",
            "alias",
            "table_id",
        ),
        MetricInfo: (
            "id",
            "name",
            "description",
            "relevant_columns",
            "alias",
        ),
        ColumnMetric: ("column_id", "metric_id"),
        ValueInfo: ("id", "value", "column_id"),
    }
    for entity, expected in expected_fields.items():
        assert is_dataclass(entity)
        assert tuple(field.name for field in fields(entity)) == expected


def _test_bm25_document() -> None:
    for text in ["Order_Amount", "成交金额"]:
        document = bm25_document(text)
        assert isinstance(document, Document)
        assert document.text == text
        assert document.model == BM25_MODEL == "Qdrant/bm25"
        assert isinstance(document.options, Bm25Config)
        assert document.options == BM25_CONFIG
        assert document.options.k == 1.2
        assert document.options.b == 0.75
        assert document.options.avg_len == 256
        assert document.options.tokenizer == TokenizerType.MULTILINGUAL
        assert document.options.language == "none"
        assert document.options.lowercase is True


async def _test_service() -> None:
    config = MetaConfig.model_validate(_config_payload())
    repository_mock = Mock(spec=MetadataRepository)
    repository_mock.get_dw_schema = AsyncMock(
        return_value={
            "dim_region": {
                "region_id": "varchar(20)",
                "province": "varchar(50)",
            }
        }
    )

    async def distinct_values(
        _table_name: str,
        column_name: str,
        limit: int,
    ) -> list[str]:
        if column_name == "region_id":
            return ["R001"]
        return ["广东省", "浙江省"] if limit == 100_000 else ["广东省"]

    repository_mock.get_distinct_values = AsyncMock(side_effect=distinct_values)
    repository_mock.upsert_metadata = AsyncMock()
    repository_mock.upsert_vectors = AsyncMock()
    repository_mock.upsert_values = AsyncMock()

    embeddings_mock = Mock()

    async def embed(texts: list[str]) -> list[list[float]]:
        return [[1.0, *([0.0] * 511)] for _ in texts]

    embeddings_mock.aembed_documents = AsyncMock(side_effect=embed)
    service = MetadataSyncService(
        cast(MetadataRepository, repository_mock),
        cast(TeiEmbeddings, embeddings_mock),
    )

    await service.sync(config)

    tables, columns, metrics, relations = (
        repository_mock.upsert_metadata.await_args.args
    )
    assert all(isinstance(table, TableInfo) for table in tables)
    assert all(isinstance(column, ColumnInfo) for column in columns)
    assert all(isinstance(metric, MetricInfo) for metric in metrics)
    assert all(isinstance(relation, ColumnMetric) for relation in relations)
    assert tables[0].id == "dim_region"
    assert [column.id for column in columns] == [
        "dim_region.region_id",
        "dim_region.province",
    ]
    assert columns[1].type == "varchar(50)"
    assert metrics[0].id == "GMV"
    assert relations == [ColumnMetric("dim_region.province", "GMV")]

    calls = [call.args for call in repository_mock.get_distinct_values.await_args_list]
    assert ("dim_region", "region_id", 10) in calls
    assert ("dim_region", "province", 10) in calls
    assert ("dim_region", "province", 100_000) in calls
    assert ("dim_region", "region_id", 100_000) not in calls

    embedded_texts = [
        text
        for call in embeddings_mock.aembed_documents.await_args_list
        for text in call.args[0]
    ]
    assert {"region_id", "省份名称", "省", "GMV", "成交总额"} <= set(
        embedded_texts
    )

    first_vector_ids = {
        call.args[0]: {point.id for point in call.args[1]}
        for call in repository_mock.upsert_vectors.await_args_list
    }
    assert set(first_vector_ids) == {COLUMN_COLLECTION, METRIC_COLLECTION}
    assert len(first_vector_ids[COLUMN_COLLECTION]) == 7
    assert len(first_vector_ids[METRIC_COLLECTION]) == 3
    assert all(
        isinstance(point_id, UUID)
        for point_ids in first_vector_ids.values()
        for point_id in point_ids
    )
    first_points = [
        point
        for call in repository_mock.upsert_vectors.await_args_list
        for point in call.args[1]
    ]
    for point in first_points:
        assert isinstance(point.vector, dict)
        assert set(point.vector) == {"", BM25_VECTOR_NAME}
        assert isinstance(point.vector[""], list)
        assert len(point.vector[""]) == 512
        document = point.vector[BM25_VECTOR_NAME]
        assert isinstance(document, Document)
        assert document.text == point.payload["text"]
        assert document.model == BM25_MODEL
        assert document.options == BM25_CONFIG
    first_documents = repository_mock.upsert_values.await_args.args[0]
    assert len({document.id for document in first_documents}) == 2
    assert first_documents == [
        ValueInfo(
            stable_value_id("dim_region.province", "广东省"),
            "广东省",
            "dim_region.province",
        ),
        ValueInfo(
            stable_value_id("dim_region.province", "浙江省"),
            "浙江省",
            "dim_region.province",
        ),
    ]

    repository_mock.upsert_vectors.reset_mock()
    repository_mock.upsert_values.reset_mock()
    config.tables[0].columns[1].alias.reverse()
    await service.sync(config)
    second_vector_ids = {
        call.args[0]: {point.id for point in call.args[1]}
        for call in repository_mock.upsert_vectors.await_args_list
    }
    assert second_vector_ids == first_vector_ids
    assert repository_mock.upsert_values.await_args.args[0] == first_documents

    for schema, expected_error in [
        ({}, "DW 表不存在"),
        ({"dim_region": {"region_id": "varchar(20)"}}, "缺少字段"),
    ]:
        invalid_repository = Mock(spec=MetadataRepository)
        invalid_repository.get_dw_schema = AsyncMock(return_value=schema)
        invalid_repository.get_distinct_values = AsyncMock()
        invalid_repository.upsert_metadata = AsyncMock()
        invalid_repository.upsert_vectors = AsyncMock()
        invalid_repository.upsert_values = AsyncMock()
        try:
            await MetadataSyncService(
                cast(MetadataRepository, invalid_repository),
                cast(TeiEmbeddings, embeddings_mock),
            ).sync(config)
        except ValueError as error:
            assert expected_error in str(error)
        else:
            raise AssertionError("DW 结构缺失时同步必须失败")
        invalid_repository.get_distinct_values.assert_not_awaited()
        invalid_repository.upsert_metadata.assert_not_awaited()
        invalid_repository.upsert_vectors.assert_not_awaited()
        invalid_repository.upsert_values.assert_not_awaited()

    async def wrong_vector_count(_texts: list[str]) -> list[list[float]]:
        return []

    async def wrong_vector_dimension(texts: list[str]) -> list[list[float]]:
        return [[0.0] * 511 for _ in texts]

    for failing_embed, expected_error in [
        (wrong_vector_count, "向量数量不匹配"),
        (wrong_vector_dimension, "512"),
    ]:
        failing_embeddings = Mock()
        failing_embeddings.aembed_documents = AsyncMock(side_effect=failing_embed)
        repository_mock.upsert_vectors.reset_mock()
        try:
            await MetadataSyncService(
                cast(MetadataRepository, repository_mock),
                cast(TeiEmbeddings, failing_embeddings),
            ).sync(config)
        except RuntimeError as error:
            assert expected_error in str(error)
        else:
            raise AssertionError("TEI 返回异常向量时同步必须失败")
        repository_mock.upsert_vectors.assert_not_awaited()


async def _test_repository() -> None:
    session = AsyncMock(spec=AsyncSession)
    qdrant_mock = Mock(spec=AsyncQdrantClient)
    qdrant_mock.collection_exists = AsyncMock(return_value=False)
    qdrant_mock.create_collection = AsyncMock()
    qdrant_mock.create_vector_name = AsyncMock()
    qdrant_mock.get_collection = AsyncMock()
    qdrant_mock.upsert = AsyncMock()

    elasticsearch_mock = Mock(spec=AsyncElasticsearch)
    indices = Mock()
    indices.exists = AsyncMock(return_value=False)
    indices.create = AsyncMock()
    indices.get_mapping = AsyncMock()
    elasticsearch_mock.indices = indices

    repository = MetadataRepository(
        session,
        cast(AsyncQdrantClient, qdrant_mock),
        cast(AsyncElasticsearch, elasticsearch_mock),
    )
    schema_result = Mock()
    schema_result.mappings.return_value = [
        {
            "table_name": "dim_region",
            "column_name": "region_id",
            "column_type": "varchar(20)",
        },
        {
            "table_name": "dim_region",
            "column_name": "province",
            "column_type": "varchar(50)",
        },
        {
            "table_name": "fact_order",
            "column_name": "order_amount",
            "column_type": "decimal(12,2)",
        },
    ]
    session.execute.return_value = schema_result
    assert await repository.get_dw_schema() == {
        "dim_region": {
            "region_id": "varchar(20)",
            "province": "varchar(50)",
        },
        "fact_order": {"order_amount": "decimal(12,2)"},
    }
    session.execute.reset_mock()

    await repository.upsert_metadata(
        [TableInfo("dim_region", "dim_region", "dim", "地区")],
        [
            ColumnInfo(
                id="dim_region.province",
                name="province",
                type="varchar(50)",
                role="dimension",
                examples=["广东省"],
                description="省份",
                alias=["省"],
                table_id="dim_region",
            )
        ],
        [
            MetricInfo(
                id="GMV",
                name="GMV",
                description="成交总额",
                relevant_columns=["dim_region.province"],
                alias=["成交总额"],
            )
        ],
        [ColumnMetric("dim_region.province", "GMV")],
    )
    assert session.execute.await_count == 4
    assert [
        execute_call.args[0].table.name
        for execute_call in session.execute.await_args_list
    ] == ["table_info", "column_info", "metric_info", "column_metric"]
    assert all(
        "ON DUPLICATE KEY UPDATE"
        in str(execute_call.args[0].compile(dialect=mysql.dialect()))
        for execute_call in session.execute.await_args_list
    )
    column_params = session.execute.await_args_list[1].args[1]
    metric_params = session.execute.await_args_list[2].args[1]
    assert isinstance(column_params[0]["examples"], list)
    assert isinstance(column_params[0]["alias"], list)
    assert isinstance(metric_params[0]["relevant_columns"], list)
    assert isinstance(metric_params[0]["alias"], list)
    assert column_params[0]["examples"] == ["广东省"]
    assert column_params[0]["alias"] == ["省"]
    assert metric_params[0]["relevant_columns"] == ["dim_region.province"]

    distinct_result = Mock()
    distinct_result.scalars.return_value = ["广东省", "浙江省"]
    session.execute.return_value = distinct_result
    assert await repository.get_distinct_values("dim_region", "province", 100000) == [
        "广东省",
        "浙江省",
    ]
    distinct_statement = str(session.execute.await_args.args[0])
    assert "CONVERT(MIN(BINARY `province`) USING utf8mb4)" in distinct_statement
    assert "GROUP BY `province`" in distinct_statement
    assert "ORDER BY MIN(BINARY `province`) LIMIT :limit" in distinct_statement
    assert session.execute.await_args.args[1] == {"limit": 100000}

    execute_count = session.execute.await_count
    try:
        await repository.get_distinct_values("bad;drop", "province", 10)
    except ValueError:
        pass
    else:
        raise AssertionError("非法动态标识符不应进入 SQL")
    assert session.execute.await_count == execute_count

    point = PointStruct(
        id=stable_point_id(COLUMN_COLLECTION, "dim_region.province", "name", "province"),
        vector={
            "": [1.0, *([0.0] * 511)],
            BM25_VECTOR_NAME: bm25_document("province"),
        },
        payload={"id": "dim_region.province"},
    )
    await repository.upsert_vectors(COLUMN_COLLECTION, [point])
    vector_config = qdrant_mock.create_collection.await_args.kwargs[
        "vectors_config"
    ]
    assert vector_config.size == 512
    assert vector_config.distance == Distance.COSINE
    sparse_config = qdrant_mock.create_collection.await_args.kwargs[
        "sparse_vectors_config"
    ][BM25_VECTOR_NAME]
    assert isinstance(sparse_config, SparseVectorParams)
    assert sparse_config.modifier == Modifier.IDF
    qdrant_mock.upsert.assert_awaited_once()

    qdrant_mock.collection_exists.return_value = True
    qdrant_mock.upsert.reset_mock()
    collection_info = Mock()
    collection_params = Mock()
    collection_info.config.params = collection_params
    collection_params.vectors = VectorParams(
        size=511,
        distance=Distance.COSINE,
    )
    collection_params.sparse_vectors = None
    qdrant_mock.get_collection.return_value = collection_info
    try:
        await repository.upsert_vectors(COLUMN_COLLECTION, [point])
    except RuntimeError as error:
        assert "512" in str(error)
    else:
        raise AssertionError("不兼容的 Qdrant collection 必须失败")
    qdrant_mock.upsert.assert_not_awaited()

    collection_params.vectors = {
        "named": VectorParams(size=512, distance=Distance.COSINE)
    }
    try:
        await repository.upsert_vectors(COLUMN_COLLECTION, [point])
    except RuntimeError as error:
        assert "512" in str(error)
    else:
        raise AssertionError("named-vector Qdrant collection 必须失败")
    qdrant_mock.upsert.assert_not_awaited()

    collection_params.vectors = VectorParams(
        size=512,
        distance=Distance.DOT,
    )
    try:
        await repository.upsert_vectors(COLUMN_COLLECTION, [point])
    except RuntimeError as error:
        assert "Cosine" in str(error)
    else:
        raise AssertionError("非 Cosine Qdrant collection 必须失败")
    qdrant_mock.upsert.assert_not_awaited()

    collection_params.vectors = VectorParams(
        size=512,
        distance=Distance.COSINE,
    )
    legacy_sparse = SparseVectorParams(modifier=Modifier.NONE)
    collection_params.sparse_vectors = {"sparse": legacy_sparse}
    await repository.upsert_vectors(COLUMN_COLLECTION, [point])
    qdrant_mock.create_vector_name.assert_awaited_once()
    create_vector = qdrant_mock.create_vector_name.await_args.kwargs
    assert create_vector["vector_name"] == BM25_VECTOR_NAME
    assert create_vector["wait"] is True
    vector_name_config = create_vector["vector_name_config"]
    assert isinstance(vector_name_config, SparseVectorNameConfig)
    assert vector_name_config.sparse.modifier == Modifier.IDF
    assert collection_params.sparse_vectors == {"sparse": legacy_sparse}
    qdrant_mock.upsert.assert_awaited_once()

    qdrant_mock.create_vector_name.reset_mock()
    qdrant_mock.upsert.reset_mock()
    collection_params.sparse_vectors = {
        BM25_VECTOR_NAME: SparseVectorParams(modifier=Modifier.IDF)
    }
    await repository.upsert_vectors(COLUMN_COLLECTION, [point])
    qdrant_mock.create_vector_name.assert_not_awaited()
    qdrant_mock.upsert.assert_awaited_once()

    qdrant_mock.upsert.reset_mock()
    collection_params.sparse_vectors = {
        BM25_VECTOR_NAME: SparseVectorParams(modifier=Modifier.NONE)
    }
    try:
        await repository.upsert_vectors(COLUMN_COLLECTION, [point])
    except RuntimeError as error:
        assert "IDF" in str(error)
    else:
        raise AssertionError("非 IDF BM25 配置必须失败")
    qdrant_mock.upsert.assert_not_awaited()

    qdrant_mock.collection_exists.reset_mock()
    try:
        await repository.upsert_vectors(
            COLUMN_COLLECTION,
            [
                PointStruct(
                    id=point.id,
                    vector={
                        "": [0.0] * 512,
                        BM25_VECTOR_NAME: bm25_document("province"),
                        "sparse": [1.0],
                    },
                    payload=point.payload,
                )
            ],
        )
    except RuntimeError as error:
        assert "sparse" in str(error)
    else:
        raise AssertionError("新点不得继续写入旧 sparse 向量")
    qdrant_mock.collection_exists.assert_not_awaited()

    try:
        await repository.upsert_vectors(
            COLUMN_COLLECTION,
            [
                PointStruct(
                    id=point.id,
                    vector={
                        "": [0.0] * 511,
                        BM25_VECTOR_NAME: bm25_document("province"),
                    },
                    payload=point.payload,
                )
            ],
        )
    except RuntimeError as error:
        assert "512" in str(error)
    else:
        raise AssertionError("错误维度向量必须在写入前失败")
    qdrant_mock.collection_exists.assert_not_awaited()

    invalid_documents = [
        Document(
            text="province",
            model="other/bm25",
            options=BM25_CONFIG,
        ),
        Document(
            text="province",
            model=BM25_MODEL,
            options=Bm25Config(
                k=1.5,
                b=0.75,
                avg_len=256,
                tokenizer=TokenizerType.MULTILINGUAL,
                language="none",
                lowercase=True,
            ),
        ),
        Document(text="", model=BM25_MODEL, options=BM25_CONFIG),
    ]
    for invalid_document in invalid_documents:
        try:
            await repository.upsert_vectors(
                COLUMN_COLLECTION,
                [
                    PointStruct(
                        id=point.id,
                        vector={
                            "": [0.0] * 512,
                            BM25_VECTOR_NAME: invalid_document,
                        },
                        payload=point.payload,
                    )
                ],
            )
        except RuntimeError as error:
            assert "固定 Qdrant/bm25 配置" in str(error)
        else:
            raise AssertionError("非法 BM25 Document 必须在写入前失败")
        qdrant_mock.collection_exists.assert_not_awaited()

    captured_actions: list[dict[str, Any]] = []

    async def capture_bulk(
        _client: AsyncElasticsearch,
        actions: Any,
        **_kwargs: Any,
    ) -> tuple[int, list[Any]]:
        captured_actions.extend(list(actions))
        return len(captured_actions), []

    document = ValueInfo(
        id=stable_value_id("dim_region.province", "广东省"),
        value="广东省",
        column_id="dim_region.province",
    )
    bulk_mock = AsyncMock(side_effect=capture_bulk)
    with patch("app.repository.metadata_repository.async_bulk", bulk_mock):
        await repository.upsert_values([document])

    mappings = indices.create.await_args.kwargs["mappings"]
    assert "mappings" not in mappings
    assert mappings["properties"]["value"]["analyzer"] == "ik_max_word"
    assert mappings["properties"]["column_id"]["type"] == "keyword"
    assert captured_actions == [
        {
            "_op_type": "index",
            "_index": "data-agent-value",
            "_id": document.id,
            "_source": {
                "id": document.id,
                "value": document.value,
                "column_id": document.column_id,
            },
        }
    ]

    indices.exists.return_value = True
    indices.get_mapping.return_value = {
        "data-agent-value": {"mappings": mappings}
    }
    captured_actions.clear()
    with patch("app.repository.metadata_repository.async_bulk", bulk_mock):
        await repository.upsert_values([document])
    indices.get_mapping.assert_awaited()
    assert captured_actions[0]["_id"] == document.id

    indices.get_mapping.return_value = {
        "data-agent-value": {
            "mappings": {
                "properties": {
                    **mappings["properties"],
                    "column_id": {"type": "text"},
                }
            }
        }
    }
    try:
        await repository.upsert_values([document])
    except RuntimeError as error:
        assert "column_id" in str(error)
        assert "mapping 不兼容" in str(error)
    else:
        raise AssertionError("已存在的错误字段值索引 mapping 必须失败")

    indices.exists.return_value = False
    bulk_error = RuntimeError("bulk failed")
    with patch(
        "app.repository.metadata_repository.async_bulk",
        AsyncMock(side_effect=bulk_error),
    ):
        try:
            await repository.upsert_values([document])
        except RuntimeError as error:
            assert error is bulk_error
        else:
            raise AssertionError("Elasticsearch bulk 异常必须原样传播")


async def _test_remote_qdrant_bm25_passthrough() -> None:
    """远端客户端转换后仍把 core BM25 Document 交给服务端。"""
    client = AsyncQdrantClient(
        url="http://127.0.0.1:6333",
        check_compatibility=False,
    )
    remote_upsert = AsyncMock()
    remote_client = cast(Any, client)._client
    try:
        with patch.object(remote_client, "upsert", remote_upsert):
            await client.upsert(
                collection_name=COLUMN_COLLECTION,
                points=[
                    PointStruct(
                        id=stable_point_id(
                            COLUMN_COLLECTION,
                            "dim_region.province",
                            "name",
                            "province",
                        ),
                        vector={
                            "": [0.0] * 512,
                            BM25_VECTOR_NAME: bm25_document("province"),
                        },
                    )
                ],
            )
    finally:
        await client.close()

    upsert_call = remote_upsert.await_args
    assert upsert_call is not None
    forwarded_points = upsert_call.kwargs["points"]
    forwarded_vector = forwarded_points[0].vector
    assert isinstance(forwarded_vector, dict)
    forwarded_document = forwarded_vector[BM25_VECTOR_NAME]
    assert isinstance(forwarded_document, Document)
    assert forwarded_document.text == "province"
    assert forwarded_document.model == BM25_MODEL
    assert forwarded_document.options == BM25_CONFIG


async def _test_script_cleanup() -> None:
    initialize_error = RuntimeError("initialize failed")
    close_error = RuntimeError("close failed")
    closed: list[str] = []

    async def close(name: str) -> None:
        await asyncio.sleep(0)
        closed.append(name)

    async def close_qdrant() -> None:
        await close("qdrant")

    async def close_elasticsearch() -> None:
        await close("elasticsearch")

    async def close_tei() -> None:
        await close("tei")

    with (
        patch(
            "app.script.sync_metadata.MysqlClientManager.initialize",
            return_value=Mock(),
        ),
        patch(
            "app.script.sync_metadata.QdrantClientManager.initialize",
            side_effect=initialize_error,
        ),
        patch(
            "app.script.sync_metadata.MysqlClientManager.close",
            AsyncMock(side_effect=close_error),
        ) as mysql_close,
        patch(
            "app.script.sync_metadata.QdrantClientManager.close",
            AsyncMock(side_effect=close_qdrant),
        ) as qdrant_close,
        patch(
            "app.script.sync_metadata.ElasticsearchClientManager.close",
            AsyncMock(side_effect=close_elasticsearch),
        ) as elasticsearch_close,
        patch(
            "app.script.sync_metadata.TeiEmbeddingClientManager.close",
            AsyncMock(side_effect=close_tei),
        ) as tei_close,
    ):
        try:
            await sync_metadata(SAMPLE_CONFIG)
        except RuntimeError as error:
            assert error is initialize_error
            assert any("close failed" in note for note in error.__notes__)
        else:
            raise AssertionError("客户端初始化异常必须原样传播")

    for close_mock in [mysql_close, qdrant_close, elasticsearch_close, tei_close]:
        close_mock.assert_awaited_once()
    assert set(closed) == {"qdrant", "elasticsearch", "tei"}

    mysql_close_error = RuntimeError("mysql close failed")
    qdrant_close_error = RuntimeError("qdrant close failed")
    session_context = AsyncMock()
    session_context.__aenter__.return_value = AsyncMock(spec=AsyncSession)
    session_context.__aexit__.return_value = False

    with (
        patch(
            "app.script.sync_metadata.MysqlClientManager.initialize",
            return_value=Mock(),
        ),
        patch(
            "app.script.sync_metadata.QdrantClientManager.initialize",
            return_value=Mock(),
        ),
        patch(
            "app.script.sync_metadata.ElasticsearchClientManager.initialize",
            return_value=Mock(),
        ),
        patch(
            "app.script.sync_metadata.TeiEmbeddingClientManager.initialize",
            return_value=Mock(),
        ),
        patch(
            "app.script.sync_metadata.MysqlClientManager.session",
            return_value=session_context,
        ),
        patch(
            "app.script.sync_metadata.MetadataSyncService.sync",
            AsyncMock(),
        ),
        patch(
            "app.script.sync_metadata.MysqlClientManager.close",
            AsyncMock(side_effect=mysql_close_error),
        ) as mysql_close,
        patch(
            "app.script.sync_metadata.QdrantClientManager.close",
            AsyncMock(side_effect=qdrant_close_error),
        ) as qdrant_close,
        patch(
            "app.script.sync_metadata.ElasticsearchClientManager.close",
            AsyncMock(),
        ) as elasticsearch_close,
        patch(
            "app.script.sync_metadata.TeiEmbeddingClientManager.close",
            AsyncMock(),
        ) as tei_close,
    ):
        try:
            await sync_metadata(SAMPLE_CONFIG)
        except BaseExceptionGroup as error_group:
            assert error_group.exceptions == (
                mysql_close_error,
                qdrant_close_error,
            )
        else:
            raise AssertionError("只有关闭异常时必须抛出 BaseExceptionGroup")

    for close_mock in [mysql_close, qdrant_close, elasticsearch_close, tei_close]:
        close_mock.assert_awaited_once()


def test_metadata_sync_service() -> None:
    """运行不依赖外部服务的元数据同步检查。"""
    _test_config()
    _test_models()
    _test_entities()
    _test_bm25_document()
    asyncio.run(_test_service())
    asyncio.run(_test_repository())
    asyncio.run(_test_remote_qdrant_bm25_passthrough())
    asyncio.run(_test_script_cleanup())


if __name__ == "__main__":
    test_metadata_sync_service()
