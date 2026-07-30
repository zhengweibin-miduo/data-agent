"""Meta 索引运行时的事务边界与权威回查测试。"""

from __future__ import annotations

from types import SimpleNamespace, TracebackType
from typing import cast

import pytest
from elasticsearch import AsyncElasticsearch
from pydantic import ValidationError
from qdrant_client.async_qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.helpers.checks import check_equal, check_exception, fail_check

from data_agent.ddl_metadata.worker.lifecycle import is_fatal_index_error
from data_agent.errors import DataAgentError
from data_agent.metadata_indexing import dispatcher as dispatcher_module
from data_agent.metadata_indexing.dispatcher import MetadataIndexDispatcher
from data_agent.metadata_indexing.elasticsearch import (
    MetadataValueElasticsearchIndex,
)
from data_agent.metadata_indexing.models import (
    ClaimedMetadataIndexWork,
    MetadataIndexOperation,
    MetadataIndexTarget,
    MetadataObjectKind,
    MetadataSemanticHit,
    MetadataSemanticProjection,
    MetadataValueProjection,
)
from data_agent.metadata_indexing.projections import MetadataProjectionRepository
from data_agent.metadata_indexing.qdrant import MetadataQdrantIndex
from data_agent.settings import AppSettings, app_config


class _Session:
    """测试用 Session 占位符。"""


class _SessionContext:
    """记录事务深度的异步上下文。"""

    def __init__(self, state: dict[str, object]) -> None:
        """绑定共享测试状态。"""
        self._state = state

    async def __aenter__(self) -> _Session:
        """进入一个事务边界。"""
        self._state["depth"] = cast(int, self._state["depth"]) + 1
        return _Session()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """离开一个事务边界。"""
        self._state["depth"] = cast(int, self._state["depth"]) - 1


async def test_dispatcher_calls_qdrant_outside_mysql_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """领取和确认使用短事务，Qdrant 与 TEI 调用不持有事务。"""
    state: dict[str, object] = {
        "depth": 0,
        "external_depths": [],
        "ack_depth": -1,
        "ack_calls": 0,
        "projection_reads": 0,
        "current_fingerprint": "v" * 64,
        "restore_calls": 0,
    }
    item = ClaimedMetadataIndexWork(
        target=MetadataIndexTarget.SEMANTIC,
        object_kind=MetadataObjectKind.COLUMN,
        object_id="column-1",
        operation=MetadataIndexOperation.UPSERT,
        desired_version="v" * 64,
        lease_token="l" * 32,
    )
    projection = MetadataSemanticProjection(
        kind=MetadataObjectKind.COLUMN,
        object_id="column-1",
        table_id="table-1",
        search_text="订单状态",
        schema_fingerprint=item.desired_version,
        projection_version="v1",
    )

    class FakeMySQLDatabase:
        """提供记录事务深度的 Session 工厂。"""

        @classmethod
        def session(cls) -> _SessionContext:
            """返回新的记录型事务上下文。"""
            return _SessionContext(state)

    class FakeOutboxRepository:
        """返回单项任务并记录确认深度。"""

        def __init__(self, session: _Session) -> None:
            """接收测试 Session。"""
            self._session = session

        async def claim(self) -> list[ClaimedMetadataIndexWork]:
            """返回一个语义 upsert。"""
            return [item]

        async def acknowledge(self, work: ClaimedMetadataIndexWork) -> bool:
            """记录确认发生在事务内。"""
            del work
            state["ack_depth"] = state["depth"]
            state["ack_calls"] = cast(int, state["ack_calls"]) + 1
            return True

        async def restore_reconciliation(
            self,
            work: ClaimedMetadataIndexWork,
        ) -> bool:
            """记录指纹变化触发的新一轮收敛。"""
            del work
            state["restore_calls"] = cast(int, state["restore_calls"]) + 1
            return True

        async def backoff(
            self,
            work: ClaimedMetadataIndexWork,
            error_type: str,
        ) -> bool:
            """成功路径不应进入退避。"""
            del work, error_type
            return False

    class FakeProjectionRepository:
        """返回固定 Meta 语义投影。"""

        def __init__(self, session: _Session) -> None:
            """接收测试 Session。"""
            self._session = session

        async def semantic_projection(
            self,
            kind: MetadataObjectKind,
            object_id: str,
        ) -> MetadataSemanticProjection:
            """返回固定投影。"""
            del kind, object_id
            state["projection_reads"] = cast(int, state["projection_reads"]) + 1
            if cast(int, state["projection_reads"]) % 2:
                return projection
            return projection.model_copy(
                update={"schema_fingerprint": state["current_fingerprint"]}
            )

    class FakeQdrantIndex:
        """记录外部写入时的事务深度。"""

        def __init__(self, client: object) -> None:
            """接收测试客户端。"""
            self._client = client

        async def upsert(
            self,
            semantic: MetadataSemanticProjection,
            vector: list[float],
        ) -> None:
            """记录 Qdrant 写入。"""
            del semantic, vector
            cast(list[int], state["external_depths"]).append(cast(int, state["depth"]))

    class FakeEmbeddings:
        """返回固定测试向量。"""

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            """记录 TEI 调用并返回固定向量。"""
            del texts
            cast(list[int], state["external_depths"]).append(cast(int, state["depth"]))
            return [[0.1]]

    class FakeClient:
        """提供固定外部客户端。"""

        @classmethod
        def get_client(cls) -> object:
            """返回固定测试客户端。"""
            return FakeEmbeddings()

    monkeypatch.setattr(dispatcher_module, "MySQLDatabase", FakeMySQLDatabase)
    monkeypatch.setattr(
        dispatcher_module,
        "MetadataIndexOutboxRepository",
        FakeOutboxRepository,
    )
    monkeypatch.setattr(
        dispatcher_module,
        "MetadataProjectionRepository",
        FakeProjectionRepository,
    )
    monkeypatch.setattr(dispatcher_module, "MetadataQdrantIndex", FakeQdrantIndex)
    monkeypatch.setattr(dispatcher_module, "QdrantClient", FakeClient)
    monkeypatch.setattr(dispatcher_module, "TEIEmbeddingClient", FakeClient)

    processed = await MetadataIndexDispatcher().dispatch()

    check_equal("成功处理一项", processed, 1)
    check_equal("外部调用均无 MySQL 事务", state["external_depths"], [0, 0])
    check_equal("确认发生在短事务内", state["ack_depth"], 1)
    check_equal("写入后重新读取当前语义指纹", state["projection_reads"], 2)

    state["current_fingerprint"] = "new-projection"
    processed = await MetadataIndexDispatcher().dispatch()

    check_equal("指纹变化的迟到写入不得确认", processed, 0)
    check_equal("指纹变化不删除当前 desired state", state["ack_calls"], 1)
    check_equal("指纹变化强制新一轮收敛", state["restore_calls"], 1)


def test_value_candidates_reject_stale_schema_fingerprint() -> None:
    """ES 值命中必须同时匹配当前字段、表和结构指纹。"""
    repository = MetadataProjectionRepository(cast(AsyncSession, object()))
    current = MetadataValueProjection(
        column_id="column-1",
        table_id="table-1",
        value_text="华东",
        value_keyword="华东",
        frequency=3,
        refresh_version="refresh-1",
        schema_fingerprint="fingerprint-current",
    )
    stale = current.model_copy(update={"schema_fingerprint": "fingerprint-old"})

    candidates = repository.authoritative_value_candidates(
        [stale, current],
        {"column-1": ("table-1", "fingerprint-current")},
    )

    check_equal("只保留当前结构值", [item.value for item in candidates], ["华东"])


async def test_semantic_candidates_reject_stale_projection_fingerprint() -> None:
    """Qdrant 语义命中必须匹配当前 Meta 权威投影指纹。"""
    current = MetadataSemanticProjection(
        kind=MetadataObjectKind.COLUMN,
        object_id="column-1",
        table_id="table-1",
        search_text="订单状态",
        schema_fingerprint="current",
        projection_version="v1",
    )

    class FakeResult:
        """返回空 pending 集合。"""

        def all(self) -> list[object]:
            """返回空结果。"""
            return []

    class FakeSession:
        """提供 pending 查询结果。"""

        async def execute(self, statement: object) -> FakeResult:
            """忽略查询并返回空 pending 集合。"""
            del statement
            return FakeResult()

    class FakeProjectionRepository(MetadataProjectionRepository):
        """返回固定当前权威投影。"""

        async def semantic_projection(
            self,
            kind: MetadataObjectKind,
            object_id: str,
        ) -> MetadataSemanticProjection:
            """返回固定当前投影。"""
            del kind, object_id
            return current

    repository = FakeProjectionRepository(cast(AsyncSession, FakeSession()))
    candidates = await repository.authoritative_candidates(
        [
            MetadataSemanticHit(
                kind=MetadataObjectKind.COLUMN,
                object_id="column-1",
                schema_fingerprint="stale",
                score=0.5,
                matched_text="订单状态",
            )
        ]
    )

    check_equal("拒绝旧语义投影", candidates, [])


def test_metadata_mapping_errors_are_fatal_at_worker_startup() -> None:
    """两个 Meta 索引 mapping 不兼容都必须阻断 worker 启动。"""
    fatal = [
        is_fatal_index_error(
            DataAgentError(code, "metadata_index_setup", "mapping invalid")
        )
        for code in (
            "metadata_semantic_mapping_invalid",
            "metadata_value_mapping_invalid",
        )
    ]

    check_equal("Meta mapping 错误均为致命错误", fatal, [True, True])


def test_metadata_indexes_must_be_isolated_from_memory_indexes() -> None:
    """配置必须拒绝 Meta 与长期记忆共用派生索引目标。"""
    for section, metadata_key, memory_key in (
        ("qdrant", "metadata_collection", "memory_collection"),
        ("elasticsearch", "metadata_value_index", "memory_index"),
    ):
        payload = app_config.model_dump(mode="python")
        payload[section][metadata_key] = payload[section][memory_key]
        try:
            AppSettings.model_validate(payload)
        except ValidationError as error:
            check_exception(f"{section} 索引隔离", error, ValidationError)
        else:
            fail_check(
                f"{section} 索引隔离",
                actual="配置被接受",
                expected="ValidationError",
            )


async def test_elasticsearch_setup_rejects_dynamic_mapping() -> None:
    """字段值索引存在但不是 strict mapping 时启动校验必须失败。"""

    class FakeIndices:
        """返回错误动态 mapping 的 Elasticsearch indices 客户端。"""

        async def exists(self, *, index: str) -> bool:
            """报告索引已存在。"""
            del index
            return True

        async def get_mapping(self, *, index: str) -> SimpleNamespace:
            """返回动态 mapping。"""
            return SimpleNamespace(
                body={index: {"mappings": {"dynamic": "true", "properties": {}}}}
            )

        async def get_settings(self, *, index: str) -> SimpleNamespace:
            """返回空分析器设置。"""
            return SimpleNamespace(body={index: {"settings": {"index": {}}}})

    client = SimpleNamespace(indices=FakeIndices())
    try:
        await MetadataValueElasticsearchIndex(cast(AsyncElasticsearch, client)).setup()
    except DataAgentError as error:
        check_equal(
            "拒绝动态字段值 mapping",
            error.code,
            "metadata_value_mapping_invalid",
        )
    else:
        fail_check(
            "拒绝动态字段值 mapping",
            actual="setup 成功",
            expected="DataAgentError",
        )


async def test_value_search_uses_bounded_fuzzy_matching() -> None:
    """字段范围内的文本查询必须启用有界编辑距离召回。"""
    captured: dict[str, object] = {}

    class FakeClient:
        """记录 Elasticsearch 查询载荷。"""

        async def search(self, **kwargs: object) -> dict[str, object]:
            """保存查询并返回空命中。"""
            captured.update(kwargs)
            return {"hits": {"hits": []}}

    index = MetadataValueElasticsearchIndex(
        cast(AsyncElasticsearch, FakeClient())
    )
    values = await index.search("Shanghi", {"column-1"}, 10)

    check_equal("模糊查询返回空测试结果", values, [])
    query = cast(dict[str, object], captured["query"])
    should = cast(dict[str, object], query["bool"])["should"]
    fuzzy = cast(list[dict[str, object]], should)[1]
    options = cast(
        dict[str, object],
        cast(dict[str, object], fuzzy["match"])["value_text"],
    )
    check_equal("启用自动编辑距离", options["fuzziness"], "AUTO")
    check_equal("限制模糊展开数量", options["max_expansions"], 50)
    check_equal("保留首字符前缀", options["prefix_length"], 1)


async def test_qdrant_setup_rejects_wrong_vector_dimension() -> None:
    """Meta 语义集合维度不一致时启动校验必须失败。"""

    class FakeQdrantClient:
        """返回错误向量维度的 Qdrant 客户端。"""

        async def collection_exists(self, collection_name: str) -> bool:
            """报告集合已存在。"""
            del collection_name
            return True

        async def get_collection(self, collection_name: str) -> SimpleNamespace:
            """返回错误 dense 向量配置。"""
            del collection_name
            params = SimpleNamespace(
                vectors={
                    "dense": SimpleNamespace(size=1, distance="Cosine"),
                },
                sparse_vectors={},
            )
            return SimpleNamespace(
                config=SimpleNamespace(params=params),
                payload_schema={},
            )

    try:
        await MetadataQdrantIndex(cast(AsyncQdrantClient, FakeQdrantClient())).setup()
    except DataAgentError as error:
        check_equal(
            "拒绝错误 Meta 向量维度",
            error.code,
            "metadata_semantic_mapping_invalid",
        )
    else:
        fail_check(
            "拒绝错误 Meta 向量维度",
            actual="setup 成功",
            expected="DataAgentError",
        )


async def test_qdrant_setup_rejects_wrong_payload_index_type() -> None:
    """Meta 语义集合 payload 索引类型漂移时必须阻断启动。"""

    class FakeQdrantClient:
        """返回错误 payload 索引类型的 Qdrant 客户端。"""

        async def collection_exists(self, collection_name: str) -> bool:
            """报告集合已存在。"""
            del collection_name
            return True

        async def get_collection(self, collection_name: str) -> SimpleNamespace:
            """返回向量正确但 kind 类型错误的集合。"""
            del collection_name
            params = SimpleNamespace(
                vectors={
                    "dense": SimpleNamespace(size=1024, distance="Cosine"),
                },
                sparse_vectors={
                    "bm25": SimpleNamespace(modifier="idf"),
                },
            )
            return SimpleNamespace(
                config=SimpleNamespace(params=params),
                payload_schema={
                    "kind": SimpleNamespace(data_type="integer"),
                },
            )

    try:
        await MetadataQdrantIndex(cast(AsyncQdrantClient, FakeQdrantClient())).setup()
    except DataAgentError as error:
        check_equal(
            "拒绝错误 Meta payload 索引类型",
            error.code,
            "metadata_semantic_mapping_invalid",
        )
    else:
        fail_check(
            "拒绝错误 Meta payload 索引类型",
            actual="setup 成功",
            expected="DataAgentError",
        )
