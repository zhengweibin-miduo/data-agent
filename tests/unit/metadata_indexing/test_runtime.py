"""Meta 索引运行时的事务边界与权威回查测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace, TracebackType
from typing import Any, cast

import pytest
from elasticsearch import AsyncElasticsearch
from pydantic import ValidationError
from qdrant_client.async_qdrant_client import AsyncQdrantClient
from sqlalchemy.dialects.mysql import dialect as mysql_dialect
from sqlalchemy.ext.asyncio import AsyncSession
from tests.helpers.checks import check_equal, check_exception, fail_check

from data_agent.ddl_metadata.worker.lifecycle import is_fatal_index_error
from data_agent.errors import DataAgentError
from data_agent.metadata_indexing import dispatcher as dispatcher_module
from data_agent.metadata_indexing import rebuilder as rebuilder_module
from data_agent.metadata_indexing.dispatcher import (
    LocalProjectionError,
    MetadataIndexDispatcher,
)
from data_agent.metadata_indexing.elasticsearch import (
    MetadataValueElasticsearchIndex,
    _async_bulk_chunks,
    metadata_value_projection_fits_bulk,
)
from data_agent.metadata_indexing.models import (
    ClaimedMetadataIndexWork,
    MetadataIndexDesired,
    MetadataIndexOperation,
    MetadataIndexTarget,
    MetadataObjectKind,
    MetadataSemanticHit,
    MetadataSemanticProjection,
    MetadataValueCandidate,
    MetadataValueProjection,
)
from data_agent.metadata_indexing.projections import (
    MetadataProjectionRepository,
    _pending_value_scope_statement,
    _safe_shared_column_names,
    _stable_value_text,
)
from data_agent.metadata_indexing.qdrant import MetadataQdrantIndex
from data_agent.metadata_indexing.rebuilder import MetadataIndexRebuilder
from data_agent.metadata_indexing.search import (
    _finalize_value_results,
    _refresh_generation_matches,
    _semantic_candidate_limit,
    _value_candidate_limit,
)
from data_agent.settings import AppSettings, app_config


class _Session:
    """测试用 Session 占位符。"""


def _value_projection(value: str) -> MetadataValueProjection:
    """构造字段值流式分块测试投影。"""
    return MetadataValueProjection(
        column_id="column-1",
        table_id="table-1",
        value_text=value,
        value_keyword=value,
        frequency=1,
        refresh_version="v1",
        schema_fingerprint="schema-1",
    )


def test_single_value_bulk_budget_is_checked_before_publication() -> None:
    """超出单文档预算的长文本必须在 publication 前确定性排除。"""
    check_equal(
        "普通字段值满足单文档预算",
        metadata_value_projection_fits_bulk(_value_projection("华东")),
        True,
    )
    check_equal(
        "超长字段值超过单文档预算",
        metadata_value_projection_fits_bulk(_value_projection("x" * (5 * 1024 * 1024))),
        False,
    )


async def test_bulk_chunks_emit_before_projection_stream_is_exhausted() -> None:
    """字段值流尚未穷尽时必须先产出首个有界 bulk。"""
    consumed = 0

    async def projections() -> AsyncIterator[MetadataValueProjection]:
        """产生超过一个 bulk 的投影，并记录已读取数量。"""
        nonlocal consumed
        for index in range(1_000):
            consumed += 1
            yield _value_projection(str(index))

    chunks = _async_bulk_chunks(projections())
    first = await anext(chunks)

    check_equal("首批文档数受预算限制", len(first) // 2, 500)
    check_equal("首批发送前未读取剩余投影", consumed, 501)


def test_semantic_search_overfetches_bounded_candidate_pool() -> None:
    """语义权威过滤前必须拉取配置允许的完整候选池。"""
    check_equal(
        "语义候选池使用配置上限",
        _semantic_candidate_limit(),
        app_config.metadata_index.search_limit,
    )


def test_value_search_overfetches_bounded_candidate_pool() -> None:
    """字段值权威过滤前必须拉取配置允许的完整候选池。"""
    check_equal(
        "字段值候选池使用配置上限",
        _value_candidate_limit(),
        app_config.metadata_index.search_limit,
    )


def test_value_scope_includes_global_rebuild_state() -> None:
    """字段值完整性必须包含未完成的全局重建任务。"""
    compiled = str(
        _pending_value_scope_statement({"table-1"}).compile(
            dialect=mysql_dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    check_equal("查询限定 VALUES 目标", "target = 'values'" in compiled, True)
    check_equal("查询包含当前表", "object_id IN ('table-1')" in compiled, True)
    check_equal("查询包含全局重建", "operation = 'rebuild'" in compiled, True)
    check_equal("查询排除已完成刷新", "phase != 'complete'" in compiled, True)


def test_set_value_text_is_stable_business_value() -> None:
    """MySQL SET 投影必须使用稳定业务文本而非 Python 容器表示。"""
    check_equal(
        "SET 值按名称排序并用逗号连接",
        _stable_value_text({"beta", "alpha"}),
        "alpha,beta",
    )


def test_bit_value_text_is_numeric_business_value() -> None:
    """MySQL BIT 投影必须按字段类型转换为可查询的十进制业务值。"""
    check_equal("BIT bytes 转十进制", _stable_value_text(b"\x05", "BIT(8)"), "5")
    check_equal("bare BIT bytes 转十进制", _stable_value_text(b"\x01", "BIT"), "1")


def test_time_value_text_uses_mysql_business_format() -> None:
    """MySQL TIME 投影必须保留可直接用于条件的有符号业务文本。"""
    check_equal(
        "普通 TIME",
        _stable_value_text(timedelta(hours=12, minutes=30), "TIME"),
        "12:30:00",
    )
    check_equal(
        "负数及微秒 TIME",
        _stable_value_text(timedelta(microseconds=-3_661_000_001), "TIME(6)"),
        "-01:01:01.000001",
    )
    check_equal(
        "超过 24 小时 TIME",
        _stable_value_text(timedelta(hours=27), "TIME"),
        "27:00:00",
    )


def test_json_value_text_is_canonical_across_mysql_and_binlog_formats() -> None:
    """MySQL 与 Binlog 的等价 JSON 文本必须生成相同频次键。"""
    mysql_text = '{"beta": 2, "alpha": {"items": [3, 1]}}'
    binlog_text = '{"alpha":{"items":[3,1]},"beta":2}'

    check_equal(
        "JSON 文本忽略空格和对象键顺序",
        _stable_value_text(mysql_text, "JSON"),
        _stable_value_text(binlog_text, "JSON"),
    )
    check_equal(
        "JSON 频次键使用紧凑稳定文本",
        _stable_value_text(mysql_text, "JSON"),
        binlog_text,
    )


def test_empty_value_hits_detect_concurrent_refresh_generation() -> None:
    """零命中也必须通过查询前后代次判断并发刷新。"""
    check_equal(
        "零命中检测到新增可见代次",
        _refresh_generation_matches({}, {"table-1": frozenset({"v2"})}, []),
        False,
    )


def test_mixed_refresh_generations_preserve_visible_partial_hits() -> None:
    """稳定混合代次中的命中属于可见集合时必须保留。"""
    visible = {"table-1": frozenset({"v1", "v2"})}

    check_equal(
        "混合代次命中仍有效",
        _refresh_generation_matches(visible, visible, [_value_projection("Shanghai")]),
        True,
    )


@pytest.mark.asyncio
async def test_visible_refresh_generations_include_every_composite_page() -> None:
    """三代以上共存时必须分页收集全部可见刷新代次。"""

    class FakeElasticsearch:
        """返回两页 composite aggregation。"""

        def __init__(self) -> None:
            """初始化请求记录。"""
            self.calls: list[dict[str, object]] = []

        async def search(self, **kwargs: object) -> dict[str, object]:
            """按 after key 返回刷新代次分页。"""
            self.calls.append(kwargs)
            composite = cast(
                dict[str, object],
                cast(dict[str, object], kwargs["aggs"])["versions"],
            )["composite"]
            after = cast(dict[str, object], composite).get("after")
            if after is None:
                return {
                    "aggregations": {
                        "versions": {
                            "buckets": [
                                {
                                    "key": {
                                        "table_id": "table-1",
                                        "refresh_version": "v0",
                                    }
                                },
                                {
                                    "key": {
                                        "table_id": "table-1",
                                        "refresh_version": "v1",
                                    }
                                },
                            ],
                            "after_key": {
                                "table_id": "table-1",
                                "refresh_version": "v1",
                            },
                        }
                    }
                }
            return {
                "aggregations": {
                    "versions": {
                        "buckets": [
                            {"key": {"table_id": "table-1", "refresh_version": "v2"}}
                        ]
                    }
                }
            }

    client = FakeElasticsearch()
    index = MetadataValueElasticsearchIndex(cast(Any, client))

    versions = await index.current_refresh_versions({"table-1"})

    check_equal(
        "完整收集三代",
        versions,
        {"table-1": frozenset({"v0", "v1", "v2"})},
    )
    check_equal("读取两页", len(client.calls), 2)


def test_incomplete_value_search_preserves_authoritative_candidates() -> None:
    """正常 pending 刷新只降低完整性，不得清空已通过权威过滤的部分值。"""
    candidate = MetadataValueCandidate(
        column_id="column-1",
        table_id="table-1",
        value="Shanghai",
        frequency=2,
    )
    scope = {"column-1": ("table-1", "schema-1")}

    values, complete = _finalize_value_results([candidate], scope, scope, True, False)

    check_equal("不完整刷新保留有效候选", values, [candidate])
    check_equal("不完整刷新正确降级", complete, False)


def test_shared_target_keeps_every_eligible_peer_column() -> None:
    """共享 DW 同名列在所有 peer 合格时必须保留逐来源逻辑投影。"""
    safe = _safe_shared_column_names(
        {
            "region": {"source-a-region", "source-b-region"},
            "status": {"source-a-status"},
        },
        {"source-a-region", "source-b-region", "source-a-status"},
    )
    check_equal("多来源同名字段通过来源归属隔离", safe, {"region", "status"})
    unsafe = _safe_shared_column_names(
        {
            "region": {"source-a-region", "source-b-region"},
            "status": {"source-a-status"},
        },
        {"source-a-region", "source-a-status"},
    )
    check_equal("任一 peer 不合格时共享字段整体关闭", unsafe, {"status"})


def test_fresh_start_uses_first_memory_versions() -> None:
    """无旧数据的新环境必须统一使用首个正式记忆版本。"""
    check_equal("用户记忆内容版本", app_config.memory.content_version, "v1")
    check_equal(
        "DDL 语义记忆内容版本",
        app_config.memory.ddl_semantic_content_version,
        "v1",
    )
    check_equal("记忆投影版本", app_config.memory.projection_version, "v1")


def test_fresh_start_uses_first_graph_version() -> None:
    """无旧检查点的新环境必须使用首个正式工作流图版本。"""
    check_equal("工作流图版本", app_config.llm.graph_version, "v1")


async def test_destructive_rebuild_persists_recovery_before_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """破坏性重建必须先持久化恢复任务并隔离 dispatcher。"""
    enqueued: list[MetadataIndexDesired] = []

    class FakeMySQLDatabase:
        """记录重建锁并提供异步上下文。"""

        @classmethod
        def session(cls) -> _SessionContext:
            del cls
            return _SessionContext({"depth": 0})

    async def fake_enqueue(self: object, desired: list[MetadataIndexDesired]) -> None:
        del self
        enqueued.extend(desired)

    monkeypatch.setattr(rebuilder_module, "MySQLDatabase", FakeMySQLDatabase)
    monkeypatch.setattr(
        rebuilder_module.MetadataIndexOutboxRepository, "enqueue", fake_enqueue
    )

    await MetadataIndexRebuilder().reset_indexes(
        confirmed_es_index=app_config.elasticsearch.metadata_value_index,
        confirmed_qdrant_collection=app_config.qdrant.metadata_collection,
    )

    check_equal("两个后端均有 durable 重建阶段", len(enqueued), 2)
    check_equal(
        "重建阶段覆盖两个目标",
        {item.target for item in enqueued},
        set(MetadataIndexTarget),
    )
    check_equal(
        "重建阶段由 dispatcher 重试",
        {item.operation for item in enqueued},
        {MetadataIndexOperation.REBUILD},
    )


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


class _LockContext:
    """不改变事务深度的命名锁异步上下文。"""

    async def __aenter__(self) -> None:
        """进入命名锁。"""

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出命名锁。"""


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
        "authoritative": True,
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

        @classmethod
        def advisory_locks(
            cls,
            names: set[str],
            *,
            timeout_seconds: float,
        ) -> _LockContext:
            """模拟语义对象命名锁。"""
            del cls, names, timeout_seconds
            return _LockContext()

    class FakeOutboxRepository:
        """返回单项任务并记录确认深度。"""

        def __init__(self, session: _Session) -> None:
            """接收测试 Session。"""
            self._session = session

        async def claim(self) -> list[ClaimedMetadataIndexWork]:
            """返回一个语义 upsert。"""
            return [item]

        async def is_authoritative(self, work: ClaimedMetadataIndexWork) -> bool:
            """模拟锁内领取身份仍然有效。"""
            del work
            return cast(bool, state["authoritative"])

        async def renew_lease(self, work: ClaimedMetadataIndexWork) -> bool:
            """在每项开始前续租并复核完整领取身份。"""
            del work
            return cast(bool, state["authoritative"])

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

    state["authoritative"] = False
    projection_reads = state["projection_reads"]
    processed = await MetadataIndexDispatcher().dispatch()

    check_equal("过期语义领取不得处理", processed, 0)
    check_equal(
        "过期语义领取不得读取或修改外部投影",
        state["projection_reads"],
        projection_reads,
    )


async def test_value_refresh_advances_one_column_then_finalizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个字段必须跨三次领取推进，且最终领取才清理旧版本。"""
    state: dict[str, object] = {
        "depth": 0,
        "progress": None,
        "upserts": [],
        "finalized": [],
        "acknowledged": 0,
        "renewals": 0,
    }
    base_item = ClaimedMetadataIndexWork(
        target=MetadataIndexTarget.VALUES,
        object_kind=MetadataObjectKind.TABLE,
        object_id="table-1",
        operation=MetadataIndexOperation.REFRESH,
        desired_version="v" * 64,
        lease_token="l" * 32,
    )

    class FakeMySQLDatabase:
        """为多次领取提供独立短事务与无状态命名锁。"""

        @classmethod
        def session(cls) -> _SessionContext:
            """返回记录型事务上下文。"""
            del cls
            return _SessionContext(state)

        @classmethod
        def advisory_locks(
            cls,
            names: set[str],
            *,
            timeout_seconds: float,
        ) -> _LockContext:
            """模拟值索引 generation lock。"""
            del cls, names, timeout_seconds
            return _LockContext()

    class FakeOutboxRepository:
        """把字段游标保存在跨 dispatch 调用共享的测试状态中。"""

        def __init__(self, session: _Session) -> None:
            """接收测试 Session。"""
            del session

        async def claim(self) -> list[ClaimedMetadataIndexWork]:
            """返回携带当前持久化游标的新领取。"""
            if cast(int, state["acknowledged"]):
                return []
            return [
                base_item.model_copy(update={"progress_column_id": state["progress"]})
            ]

        async def is_authoritative(self, item: ClaimedMetadataIndexWork) -> bool:
            """模拟当前领取仍然有效。"""
            del item
            return True

        async def renew_lease(self, item: ClaimedMetadataIndexWork) -> bool:
            """模拟每个外部边界续租成功。"""
            del item
            state["renewals"] = cast(int, state["renewals"]) + 1
            return True

        async def advance_progress(
            self,
            item: ClaimedMetadataIndexWork,
            column_id: str,
        ) -> bool:
            """保存本次唯一完成的字段。"""
            del item
            state["progress"] = column_id
            return True

        async def acknowledge(self, item: ClaimedMetadataIndexWork) -> bool:
            """记录 finalize 后的表级确认。"""
            del item
            state["acknowledged"] = cast(int, state["acknowledged"]) + 1
            return True

        async def restore_reconciliation(
            self,
            item: ClaimedMetadataIndexWork,
        ) -> bool:
            """成功路径不应补发修复代次。"""
            del item
            return False

        async def defer(
            self,
            item: ClaimedMetadataIndexWork,
            seconds: int = 30,
        ) -> bool:
            """成功路径不应延后。"""
            del item, seconds
            return False

        async def backoff(
            self,
            item: ClaimedMetadataIndexWork,
            error_type: str,
        ) -> bool:
            """成功路径不应退避。"""
            del item, error_type
            return False

    class FakeValueRefresh:
        """模拟每次只推进一个持久化工作单元。"""

        async def run_next_unit(self, item: ClaimedMetadataIndexWork) -> bool:
            """依次推进两个字段和最终 cleanup。"""
            del self
            if state["progress"] is None:
                state["progress"] = "column-1"
                cast(list[str], state["upserts"]).append("column-1")
                return False
            if state["progress"] == "column-1":
                state["progress"] = "column-2"
                cast(list[str], state["upserts"]).append("column-2")
                return False
            cast(list[tuple[str, str]], state["finalized"]).append(
                (item.object_id, item.desired_version)
            )
            state["acknowledged"] = 1
            return True

    monkeypatch.setattr(dispatcher_module, "MySQLDatabase", FakeMySQLDatabase)
    monkeypatch.setattr(
        dispatcher_module,
        "MetadataIndexOutboxRepository",
        FakeOutboxRepository,
    )
    monkeypatch.setattr(dispatcher_module, "MetadataValueRefresh", FakeValueRefresh)

    dispatcher = MetadataIndexDispatcher()
    counts = [await dispatcher.dispatch() for _ in range(3)]

    check_equal("每次 dispatch 只完成一个有界工作单元", counts, [1, 1, 1])
    check_equal("每个字段仅写入一次", state["upserts"], ["column-1", "column-2"])
    check_equal("持久化游标停在最后字段", state["progress"], "column-2")
    check_equal(
        "最后字段游标落盘后的领取才清理旧版本",
        state["finalized"],
        [("table-1", "v" * 64)],
    )
    check_equal("COMPLETE 状态持久化一次", state["acknowledged"], 1)


async def test_value_refresh_cancellation_propagates_without_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """任务取消必须原样传播，由租约到期恢复，不能记作远程失败。"""
    item = ClaimedMetadataIndexWork(
        target=MetadataIndexTarget.VALUES,
        object_kind=MetadataObjectKind.TABLE,
        object_id="table-1",
        operation=MetadataIndexOperation.REFRESH,
        desired_version="v" * 64,
        lease_token="l" * 32,
        progress_column_id="column-1",
    )

    async def cancel(
        self: MetadataIndexDispatcher,
        work: ClaimedMetadataIndexWork,
    ) -> bool:
        del self, work
        raise asyncio.CancelledError

    monkeypatch.setattr(MetadataIndexDispatcher, "_synchronize_values", cancel)

    with pytest.raises(asyncio.CancelledError):
        await MetadataIndexDispatcher()._synchronize(item)


async def test_local_progress_failure_defers_without_remote_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """进度 CAS 的本地 MySQL 失败只延后任务，不增加远程失败次数。"""
    state: dict[str, object] = {"depth": 0, "deferred": 0, "backoffs": 0}
    item = ClaimedMetadataIndexWork(
        target=MetadataIndexTarget.VALUES,
        object_kind=MetadataObjectKind.TABLE,
        object_id="table-1",
        operation=MetadataIndexOperation.REFRESH,
        desired_version="v" * 64,
        lease_token="l" * 32,
    )

    class FakeMySQLDatabase:
        """提供错误结算所需的短事务。"""

        @classmethod
        def session(cls) -> _SessionContext:
            del cls
            return _SessionContext(state)

    class FakeOutboxRepository:
        """记录本地失败使用 defer 而非 backoff。"""

        def __init__(self, session: _Session) -> None:
            del session

        async def defer(self, work: ClaimedMetadataIndexWork) -> bool:
            del work
            state["deferred"] = cast(int, state["deferred"]) + 1
            return True

        async def backoff(
            self,
            work: ClaimedMetadataIndexWork,
            error_type: str,
        ) -> bool:
            del work, error_type
            state["backoffs"] = cast(int, state["backoffs"]) + 1
            return True

    async def fail_progress(
        self: MetadataIndexDispatcher,
        work: ClaimedMetadataIndexWork,
    ) -> bool:
        del self, work
        raise LocalProjectionError

    monkeypatch.setattr(dispatcher_module, "MySQLDatabase", FakeMySQLDatabase)
    monkeypatch.setattr(
        dispatcher_module,
        "MetadataIndexOutboxRepository",
        FakeOutboxRepository,
    )
    monkeypatch.setattr(MetadataIndexDispatcher, "_synchronize_values", fail_progress)

    check_equal(
        "本地进度失败未完成任务",
        await MetadataIndexDispatcher()._synchronize(item),
        False,
    )
    check_equal("本地进度失败无损延后", state["deferred"], 1)
    check_equal("本地进度失败不消耗远程预算", state["backoffs"], 0)


async def test_cleanup_uses_explicit_document_ids() -> None:
    """旧集合清理只能使用有界 bulk 明确删除稳定文档 ID。"""
    operations: list[dict[str, object]] = []

    class FakeClient:
        """记录 bulk 删除操作。"""

        async def bulk(self, **kwargs: object) -> dict[str, object]:
            operations.extend(cast(list[dict[str, object]], kwargs["operations"]))
            return {"errors": False}

    await MetadataValueElasticsearchIndex(
        cast(AsyncElasticsearch, FakeClient())
    ).delete_documents(["doc-1", "doc-2"])

    check_equal(
        "cleanup 仅包含明确 ID",
        [item["delete"]["_id"] for item in operations],  # type: ignore[index]
        ["doc-1", "doc-2"],
    )


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

        async def _semantic_candidate_rows(
            self,
            identities: list[MetadataSemanticHit],
        ) -> tuple[
            dict[tuple[MetadataObjectKind, str], MetadataSemanticProjection],
            dict[tuple[MetadataObjectKind, str], dict[str, object]],
        ]:
            """模拟批量回读当前投影和展示内容。"""
            del identities
            key = (MetadataObjectKind.COLUMN, "column-1")
            return {key: current}, {key: {"name": "订单状态", "table_id": "table-1"}}

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


async def test_elasticsearch_setup_rejects_analyzer_filter_drift() -> None:
    """字段值 analyzer 任一组成部分漂移时必须阻断启动。"""

    class FakeIndices:
        """返回 mapping 正确但 analyzer filter 漂移的配置。"""

        async def exists(self, *, index: str) -> bool:
            """报告索引已存在。"""
            del index
            return True

        async def get_mapping(self, *, index: str) -> SimpleNamespace:
            """返回当前代码声明的严格 mapping。"""
            mappings = MetadataValueElasticsearchIndex(
                cast(AsyncElasticsearch, object())
            )._mappings()
            return SimpleNamespace(body={index: {"mappings": mappings}})

        async def get_settings(self, *, index: str) -> SimpleNamespace:
            """返回额外 lowercase filter 的不兼容 analyzer。"""
            return SimpleNamespace(
                body={
                    index: {
                        "settings": {
                            "index": {
                                "analysis": {
                                    "analyzer": {
                                        "metadata_value_zh": {
                                            "type": "custom",
                                            "tokenizer": (
                                                app_config.elasticsearch.analyzer
                                            ),
                                            "filter": ["lowercase"],
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            )

    client = SimpleNamespace(indices=FakeIndices())
    try:
        await MetadataValueElasticsearchIndex(cast(AsyncElasticsearch, client)).setup()
    except DataAgentError as error:
        check_equal(
            "拒绝 analyzer filter 漂移",
            error.code,
            "metadata_value_mapping_invalid",
        )
    else:
        fail_check(
            "拒绝 analyzer filter 漂移",
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

    index = MetadataValueElasticsearchIndex(cast(AsyncElasticsearch, FakeClient()))
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
