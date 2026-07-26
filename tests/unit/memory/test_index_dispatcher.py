"""记忆索引调度器的事务边界与收敛语义测试。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from types import TracebackType

import pytest

from data_agent.memory.indexing import dispatcher as dispatcher_module
from data_agent.memory.indexing.dispatcher import MemoryIndexDispatcher
from data_agent.models.memory import (
    BuiltinMemoryCategory,
    MemoryIndexOperation,
    MemoryIndexTarget,
    MemoryLifecyclePolicy,
    MemoryOutboxItem,
    MemoryProjection,
    MemoryStatus,
    MemoryTrust,
)
from data_agent.settings import app_config
from tests.helpers.checks import check_condition, check_equal


class _Recorder:
    """按发生顺序记录事务与外部调用事件。"""

    def __init__(self) -> None:
        """初始化事件序列与事务深度轨迹。"""
        self.events: list[str] = []
        self.depth = 0
        self.external_depths: list[int] = []
        self.event_depths: list[tuple[str, int]] = []

    def record(self, event: str) -> None:
        """登记一个普通事件及其发生时的事务深度。"""
        self.events.append(event)
        self.event_depths.append((event, self.depth))

    def record_external(self, event: str) -> None:
        """登记一次外部调用及其发生时的事务深度。"""
        self.events.append(event)
        self.event_depths.append((event, self.depth))
        self.external_depths.append(self.depth)

    def depths_for(self, event: str) -> list[int]:
        """返回指定事件每次发生时的事务深度。"""
        return [depth for name, depth in self.event_depths if name == event]


class _FakeSession:
    """测试用异步 Session 占位符。"""


class _FakeSessionContext:
    """记录事务开启与提交顺序的会话上下文。"""

    def __init__(self, recorder: _Recorder) -> None:
        """绑定共享事件记录器。"""
        self._recorder = recorder

    async def __aenter__(self) -> _FakeSession:
        """进入事务并加深事务层级。"""
        self._recorder.depth += 1
        self._recorder.record("session_open")
        return _FakeSession()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """离开事务并恢复事务层级。"""
        self._recorder.depth -= 1
        self._recorder.record("session_close")


def _projection(uid: str, status: MemoryStatus) -> MemoryProjection:
    """构造指定状态的权威投影。"""
    moment = datetime.now(UTC).replace(tzinfo=None)
    return MemoryProjection(
        memory_uid=uid,
        source="dw",
        category=BuiltinMemoryCategory.DDL_SEMANTIC.value,
        memory_key="orders",
        content_schema="semantic.v1",
        memory_text="订单事实表",
        content_hash="hash",
        object_ids=["orders"],
        trust=MemoryTrust.MODEL_VALIDATED,
        status=status,
        importance_score=0.5,
        lifecycle_policy=MemoryLifecyclePolicy.FINGERPRINT_BOUND,
        record_version=1,
        content_version=app_config.memory.content_version,
        projection_version=app_config.memory.projection_version,
        created_at=moment,
        updated_at=moment,
    )


def _items(uid: str, operation: MemoryIndexOperation) -> list[MemoryOutboxItem]:
    """构造同一 UID 的双目标期望状态。"""
    return [
        MemoryOutboxItem(
            memory_uid=uid,
            target=target,
            operation=operation,
            projection_version=app_config.memory.projection_version,
            attempts=0,
        )
        for target in MemoryIndexTarget
    ]


def _install(
    monkeypatch: pytest.MonkeyPatch,
    recorder: _Recorder,
    *,
    items: list[MemoryOutboxItem],
    projections: dict[str, MemoryProjection | None],
    failing: set[MemoryIndexTarget] | None = None,
    superseded: bool = False,
    acknowledged_hashes: list[str | None] | None = None,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """安装记录型替身并返回确认与退避轨迹。"""
    acknowledged: list[tuple[str, str]] = []
    retried: list[tuple[str, str]] = []
    acknowledged_hashes = acknowledged_hashes if acknowledged_hashes is not None else []
    broken = failing or set()

    class FakeMySQLDatabase:
        """提供记录型 MySQL 会话工厂。"""

        @classmethod
        def session(cls) -> _FakeSessionContext:
            """创建记录事务边界的会话上下文。"""
            return _FakeSessionContext(recorder)

    class FakeOutboxRepository:
        """模拟期望状态仓储并记录事务内调用。"""

        def __init__(self, session: _FakeSession) -> None:
            """记录测试 Session。"""
            self._session = session

        async def claim_outbox(self, limit: int) -> list[MemoryOutboxItem]:
            """返回预置的已领取期望状态。"""
            recorder.record("claim")
            return items

        async def projection(self, uid: str) -> MemoryProjection | None:
            """返回预置的权威投影。"""
            recorder.record("projection")
            return projections.get(uid)

        async def dead_letter_count(self) -> int:
            """本用例不构造死信积压。"""
            return 0

        async def acknowledge_outbox(
            self,
            item: MemoryOutboxItem,
            *,
            content_hash: str | None,
        ) -> bool:
            """记录被确认的期望状态与本次写入的内容哈希。"""
            recorder.record("acknowledge")
            if superseded:
                return False
            acknowledged.append((item.memory_uid, item.target.value))
            acknowledged_hashes.append(content_hash)
            return True

        async def retry_outbox(
            self,
            item: MemoryOutboxItem,
            error_type: str,
            max_backoff_seconds: int,
        ) -> None:
            """记录被退避的期望状态。"""
            recorder.record("retry")
            retried.append((item.memory_uid, item.target.value))

    class FakeElasticsearchIndex:
        """记录 Elasticsearch 外部写入。"""

        def __init__(self, client: object) -> None:
            """记录初始化客户端。"""
            self._client = client

        async def upsert(self, projection: MemoryProjection) -> None:
            """记录一次全文写入。"""
            recorder.record_external("es_upsert")
            if MemoryIndexTarget.ELASTICSEARCH in broken:
                raise TimeoutError("elasticsearch timeout")

        async def delete(self, uid: str) -> None:
            """记录一次全文删除。"""
            recorder.record_external("es_delete")

    class FakeQdrantIndex:
        """记录 Qdrant 外部写入。"""

        def __init__(self, client: object) -> None:
            """记录初始化客户端。"""
            self._client = client

        async def upsert(
            self,
            projection: MemoryProjection,
            vector: Sequence[float],
        ) -> None:
            """记录一次向量写入。"""
            recorder.record_external("qdrant_upsert")
            if MemoryIndexTarget.QDRANT in broken:
                raise TimeoutError("qdrant timeout")

        async def delete(self, uid: str) -> None:
            """记录一次向量删除。"""
            recorder.record_external("qdrant_delete")

    class FakeEmbeddings:
        """记录 TEI 向量化调用。"""

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            """返回固定向量。"""
            recorder.record_external("tei_embed")
            return [[0.1, 0.2]]

    class FakeClientProvider:
        """模拟基础设施客户端提供者。"""

        @classmethod
        def get_client(cls) -> object:
            """返回可传给索引封装的客户端。"""
            return FakeEmbeddings()

    monkeypatch.setattr(dispatcher_module, "MySQLDatabase", FakeMySQLDatabase)
    monkeypatch.setattr(
        dispatcher_module,
        "MemoryIndexOutboxRepository",
        FakeOutboxRepository,
    )
    monkeypatch.setattr(dispatcher_module, "ElasticsearchClient", FakeClientProvider)
    monkeypatch.setattr(dispatcher_module, "QdrantClient", FakeClientProvider)
    monkeypatch.setattr(dispatcher_module, "TEIEmbeddingClient", FakeClientProvider)
    monkeypatch.setattr(
        dispatcher_module,
        "MemoryElasticsearchIndex",
        FakeElasticsearchIndex,
    )
    monkeypatch.setattr(dispatcher_module, "MemoryQdrantIndex", FakeQdrantIndex)
    return acknowledged, retried


async def test_dispatch_keeps_external_calls_outside_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部写入不得发生在持有 MySQL 行锁的事务内。"""
    recorder = _Recorder()
    hashes: list[str | None] = []
    projection = _projection("memory-1", MemoryStatus.ACTIVE)
    acknowledged, retried = _install(
        monkeypatch,
        recorder,
        items=_items("memory-1", MemoryIndexOperation.UPSERT),
        projections={"memory-1": projection},
        acknowledged_hashes=hashes,
    )

    processed = await MemoryIndexDispatcher().dispatch()

    check_equal("已处理项数", processed, 2)
    check_equal(
        "外部调用发生时的事务深度",
        recorder.external_depths,
        [0, 0, 0],
    )
    check_equal("领取仍在事务内完成", recorder.depths_for("claim"), [1])
    check_equal("确认仍在事务内完成", recorder.depths_for("acknowledge"), [1, 1])
    check_condition(
        "领取事务在首次外部调用前已提交",
        recorder.events.index("session_close") < recorder.events.index("es_upsert"),
        actual=str(recorder.events),
        expected="session_close 先于 es_upsert",
    )
    check_equal(
        "两个目标各自独立确认",
        sorted(acknowledged),
        [
            ("memory-1", MemoryIndexTarget.ELASTICSEARCH.value),
            ("memory-1", MemoryIndexTarget.QDRANT.value),
        ],
    )
    check_equal(
        "确认时携带本次写入的内容哈希",
        hashes,
        [projection.content_hash, projection.content_hash],
    )
    check_equal("成功路径不产生退避", retried, [])


async def test_dispatch_converges_upsert_for_inactive_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """权威行非 ACTIVE 时 UPSERT 必须收敛为派生索引删除。"""
    recorder = _Recorder()
    acknowledged, _ = _install(
        monkeypatch,
        recorder,
        items=_items("memory-2", MemoryIndexOperation.UPSERT),
        projections={"memory-2": _projection("memory-2", MemoryStatus.DELETED)},
    )

    processed = await MemoryIndexDispatcher().dispatch()

    check_equal("已处理项数", processed, 2)
    check_equal(
        "失效行收敛为双目标删除",
        [event for event in recorder.events if event.endswith(("upsert", "delete"))],
        ["es_delete", "qdrant_delete"],
    )
    check_equal("收敛后仍确认期望状态", len(acknowledged), 2)


async def test_dispatch_converges_upsert_for_missing_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """权威行已被物理清理时 UPSERT 不得空确认。"""
    recorder = _Recorder()
    acknowledged, _ = _install(
        monkeypatch,
        recorder,
        items=_items("memory-3", MemoryIndexOperation.UPSERT),
        projections={"memory-3": None},
    )

    await MemoryIndexDispatcher().dispatch()

    check_equal(
        "缺失权威行收敛为双目标删除",
        [event for event in recorder.events if event.endswith(("upsert", "delete"))],
        ["es_delete", "qdrant_delete"],
    )
    check_equal("删除路径不调用向量化", "tei_embed" in recorder.events, False)
    check_equal("收敛后仍确认期望状态", len(acknowledged), 2)


async def test_dispatch_isolates_single_target_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单目标失败只退避自身，另一目标仍完成确认。"""
    recorder = _Recorder()
    acknowledged, retried = _install(
        monkeypatch,
        recorder,
        items=_items("memory-4", MemoryIndexOperation.UPSERT),
        projections={"memory-4": _projection("memory-4", MemoryStatus.ACTIVE)},
        failing={MemoryIndexTarget.ELASTICSEARCH},
    )

    processed = await MemoryIndexDispatcher().dispatch()

    check_equal("失败目标不计入已处理", processed, 1)
    check_equal(
        "仅失败目标进入退避",
        retried,
        [("memory-4", MemoryIndexTarget.ELASTICSEARCH.value)],
    )
    check_equal(
        "成功目标仍完成确认",
        acknowledged,
        [("memory-4", MemoryIndexTarget.QDRANT.value)],
    )
    check_equal("退避在独立短事务内完成", recorder.depths_for("retry"), [1])
    check_equal("确认在独立短事务内完成", recorder.depths_for("acknowledge"), [1])
    check_condition(
        "退避与确认使用彼此独立的事务",
        recorder.events.index("retry")
        < recorder.events.index("session_open", recorder.events.index("retry")),
        actual=str(recorder.events),
        expected="retry 之后重新开启事务再确认",
    )


async def test_dispatch_does_not_acknowledge_superseded_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """权威内容在写入期间再次变更时不得确认期望状态。"""
    recorder = _Recorder()
    acknowledged, retried = _install(
        monkeypatch,
        recorder,
        items=_items("memory-5", MemoryIndexOperation.UPSERT),
        projections={"memory-5": _projection("memory-5", MemoryStatus.ACTIVE)},
        superseded=True,
    )

    processed = await MemoryIndexDispatcher().dispatch()

    check_equal("未确认的同步不计入已处理", processed, 0)
    check_equal("被取代的期望状态保持未确认", acknowledged, [])
    check_equal("被取代不等于失败，不进入退避", retried, [])
