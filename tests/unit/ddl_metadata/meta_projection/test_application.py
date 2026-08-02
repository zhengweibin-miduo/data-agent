"""Meta Projection 应用用例公共 seam 测试。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, cast

import pytest
from loguru import logger

from data_agent.ddl_metadata.meta_projection.application.contracts import (
    ProjectionNotReadyError,
    ProjectionReader,
    ProjectionRebuilder,
    ProjectionWorkStore,
    SemanticIndex,
    ValueIndex,
    ValueRefreshPersistenceError,
    ValueRefreshRunner,
)
from data_agent.ddl_metadata.meta_projection.application.dispatcher import (
    MetadataIndexDispatcher,
)
from data_agent.ddl_metadata.meta_projection.application.rebuild import (
    MetadataIndexRebuilder,
)
from data_agent.ddl_metadata.meta_projection.application.search import (
    MetadataSearchService,
)
from data_agent.ddl_metadata.meta_projection.models import (
    ClaimedMetadataIndexWork,
    MetadataIndexOperation,
    MetadataIndexTarget,
    MetadataObjectKind,
    MetadataSemanticHit,
    MetadataSemanticProjection,
    MetadataValueCandidate,
    MetadataValueProjection,
)


def _semantic_work() -> ClaimedMetadataIndexWork:
    """构造一个语义投影领取。"""
    return ClaimedMetadataIndexWork(
        target=MetadataIndexTarget.SEMANTIC,
        object_kind=MetadataObjectKind.COLUMN,
        object_id="column-1",
        operation=MetadataIndexOperation.UPSERT,
        desired_version="d" * 64,
        lease_token="l" * 32,
    )


@dataclass
class _WorkStore:
    """记录领取、权威保护和结算结果的内存 adapter。"""

    item: ClaimedMetadataIndexWork
    events: list[str] = field(default_factory=list)
    authoritative: bool = True
    dead_letters: int = 0

    async def claim(self, limit: int) -> list[ClaimedMetadataIndexWork]:
        """返回一个有界领取。"""
        self.events.append(f"claim:{limit}")
        return [self.item]

    @asynccontextmanager
    async def authority(self, item: ClaimedMetadataIndexWork) -> AsyncIterator[bool]:
        """模拟锁内完整权威身份仍有效。"""
        assert item == self.item
        self.events.append("authority")
        yield self.authoritative

    async def acknowledge(self, item: ClaimedMetadataIndexWork) -> bool:
        """确认当前 desired state。"""
        assert item == self.item
        self.events.append("acknowledge")
        return True

    async def restore_reconciliation(self, item: ClaimedMetadataIndexWork) -> bool:
        """记录迟到写入修复。"""
        assert item == self.item
        self.events.append("restore")
        return True

    async def defer(self, item: ClaimedMetadataIndexWork) -> bool:
        """记录本地无损延后。"""
        assert item == self.item
        self.events.append("defer")
        return True

    async def backoff(self, item: ClaimedMetadataIndexWork, error_type: str) -> bool:
        """记录远程失败退避。"""
        assert item == self.item
        self.events.append(f"backoff:{error_type}")
        return True

    async def dead_letter_count(self) -> int:
        """返回配置的死信积压数量。"""
        return self.dead_letters


@dataclass
class _Reader:
    """返回稳定权威投影的内存 adapter。"""

    events: list[str]

    async def semantic_projection(
        self,
        kind: MetadataObjectKind,
        object_id: str,
    ) -> MetadataSemanticProjection:
        """返回当前语义投影。"""
        self.events.append("read")
        return MetadataSemanticProjection(
            kind=kind,
            object_id=object_id,
            table_id="table-1",
            search_text="订单状态",
            schema_fingerprint="f" * 64,
            projection_version="v1",
        )


@dataclass
class _SemanticIndex:
    """记录远程写入的内存 adapter。"""

    events: list[str]

    async def upsert(self, projection: MetadataSemanticProjection) -> None:
        """记录语义写入。"""
        del projection
        self.events.append("remote")

    async def delete(self, kind: MetadataObjectKind, object_id: str) -> None:
        """记录语义删除。"""
        del kind, object_id
        self.events.append("delete")


class _Unused:
    """任何调用都会使测试失败的 adapter。"""

    def __getattr__(self, name: str) -> object:
        """拒绝未预期调用。"""
        raise AssertionError(name)


def _build_dispatcher(
    *,
    work_store: object,
    reader: object,
    semantic_index: object,
    value_refresh: object,
    rebuilder: object,
) -> MetadataIndexDispatcher:
    """把窄测试 adapter 显式绑定到 dispatcher 端口。"""
    return MetadataIndexDispatcher(
        work_store=cast(ProjectionWorkStore, work_store),
        reader=cast(ProjectionReader, reader),
        semantic_index=cast(SemanticIndex, semantic_index),
        value_refresh=cast(ValueRefreshRunner, value_refresh),
        rebuilder=cast(ProjectionRebuilder, rebuilder),
    )


def _build_search_service(
    *,
    reader: object,
    semantic_index: object,
    value_index: object,
    search_limit: int,
) -> MetadataSearchService:
    """把窄测试 adapter 显式绑定到 search 端口。"""
    return MetadataSearchService(
        reader=cast(ProjectionReader, reader),
        semantic_index=cast(SemanticIndex, semantic_index),
        value_index=cast(ValueIndex, value_index),
        search_limit=search_limit,
    )


def _build_rebuilder(
    *,
    work_store: object,
    reader: object,
    semantic_index: object,
    value_index: object,
    projection_version: str,
    es_index: str,
    qdrant_collection: str,
) -> MetadataIndexRebuilder:
    """把窄测试 adapter 显式绑定到 rebuild 端口。"""
    return MetadataIndexRebuilder(
        work_store=cast(ProjectionWorkStore, work_store),
        reader=cast(ProjectionReader, reader),
        semantic_index=cast(SemanticIndex, semantic_index),
        value_index=cast(ValueIndex, value_index),
        projection_version=projection_version,
        es_index=es_index,
        qdrant_collection=qdrant_collection,
    )


async def test_dispatch_claims_remotely_writes_then_settles() -> None:
    """Dispatcher 必须通过公共 seam 完成 claim-remote-settle。"""
    events: list[str] = []
    store = _WorkStore(_semantic_work(), events)
    dispatcher = _build_dispatcher(
        work_store=store,
        reader=_Reader(events),
        semantic_index=_SemanticIndex(events),
        value_refresh=_Unused(),
        rebuilder=_Unused(),
    )

    processed = await dispatcher.dispatch(limit=1)

    assert processed == 1
    assert events == [
        "claim:1",
        "authority",
        "read",
        "remote",
        "read",
        "acknowledge",
    ]


async def test_dispatch_propagates_cancellation_without_backoff() -> None:
    """取消必须原样传播并由租约到期恢复。"""
    events: list[str] = []
    store = _WorkStore(_semantic_work(), events)

    class CancellingSemanticIndex(_SemanticIndex):
        """在远程边界抛出取消。"""

        async def upsert(self, projection: MetadataSemanticProjection) -> None:
            """模拟任务取消。"""
            del projection
            raise asyncio.CancelledError

    dispatcher = _build_dispatcher(
        work_store=store,
        reader=_Reader(events),
        semantic_index=CancellingSemanticIndex(events),
        value_refresh=_Unused(),
        rebuilder=_Unused(),
    )

    with pytest.raises(asyncio.CancelledError):
        await dispatcher.dispatch(limit=1)

    assert not any(event.startswith("backoff:") for event in events)


async def test_dispatch_skips_work_after_authority_loss() -> None:
    """失去完整 desired identity 权威后不得读取或写入派生投影。"""
    events: list[str] = []
    store = _WorkStore(_semantic_work(), events, authoritative=False)
    dispatcher = _build_dispatcher(
        work_store=store,
        reader=_Unused(),
        semantic_index=_Unused(),
        value_refresh=_Unused(),
        rebuilder=_Unused(),
    )

    assert await dispatcher.dispatch(limit=1) == 0
    assert events == ["claim:1", "authority"]


async def test_dispatch_backs_off_remote_failure() -> None:
    """远程写入失败必须记录安全异常类型并进入有界退避。"""
    events: list[str] = []
    store = _WorkStore(_semantic_work(), events)

    class FailingSemanticIndex(_SemanticIndex):
        """模拟语义索引连接失败。"""

        async def upsert(self, projection: MetadataSemanticProjection) -> None:
            """抛出远程连接错误。"""
            del projection
            raise ConnectionError("remote unavailable")

    dispatcher = _build_dispatcher(
        work_store=store,
        reader=_Reader(events),
        semantic_index=FailingSemanticIndex(events),
        value_refresh=_Unused(),
        rebuilder=_Unused(),
    )

    assert await dispatcher.dispatch(limit=1) == 0
    assert events == [
        "claim:1",
        "authority",
        "read",
        "backoff:ConnectionError",
    ]


async def test_report_dead_letters_logs_backlog_count() -> None:
    """达到最大失败次数的积压必须通过公共报告 seam 可见。"""
    messages: list[str] = []
    sink_id = logger.add(messages.append, format="{message}")
    try:
        dispatcher = _build_dispatcher(
            work_store=_WorkStore(_semantic_work(), dead_letters=3),
            reader=_Unused(),
            semantic_index=_Unused(),
            value_refresh=_Unused(),
            rebuilder=_Unused(),
        )

        await dispatcher.report_dead_letters()
    finally:
        logger.remove(sink_id)

    assert any("待处理项数：3" in message for message in messages)


async def test_dispatch_defers_not_ready_value_refresh() -> None:
    """本地未就绪必须 defer，不能消耗远程 retry budget。"""
    events: list[str] = []
    item = _semantic_work().model_copy(
        update={
            "target": MetadataIndexTarget.VALUES,
            "object_kind": MetadataObjectKind.TABLE,
            "object_id": "table-1",
            "operation": MetadataIndexOperation.REFRESH,
        }
    )
    store = _WorkStore(item, events)

    class NotReadyRefresh:
        """模拟 DW 尚未物化。"""

        async def run_next_unit(self, work: ClaimedMetadataIndexWork) -> bool:
            """抛出稳定的本地未就绪错误。"""
            del work
            raise ProjectionNotReadyError

    dispatcher = _build_dispatcher(
        work_store=store,
        reader=_Unused(),
        semantic_index=_Unused(),
        value_refresh=NotReadyRefresh(),
        rebuilder=_Unused(),
    )

    assert await dispatcher.dispatch(limit=1) == 0
    assert events == ["claim:1", "authority", "defer"]


async def test_dispatch_defers_value_refresh_persistence_failure() -> None:
    """本地刷新持久化失败必须 defer，不能消耗远程 retry budget。"""
    events: list[str] = []
    item = _semantic_work().model_copy(
        update={
            "target": MetadataIndexTarget.VALUES,
            "object_kind": MetadataObjectKind.TABLE,
            "object_id": "table-1",
            "operation": MetadataIndexOperation.REFRESH,
        }
    )
    store = _WorkStore(item, events)

    class FailingPersistenceRefresh:
        """模拟字段值状态机的本地持久化失败。"""

        async def run_next_unit(self, work: ClaimedMetadataIndexWork) -> bool:
            """抛出稳定的本地持久化错误。"""
            del work
            raise ValueRefreshPersistenceError

    dispatcher = _build_dispatcher(
        work_store=store,
        reader=_Unused(),
        semantic_index=_Unused(),
        value_refresh=FailingPersistenceRefresh(),
        rebuilder=_Unused(),
    )

    assert await dispatcher.dispatch(limit=1) == 0
    assert events == ["claim:1", "authority", "defer"]


@dataclass
class _SearchReader:
    """提供搜索权威回读的内存 adapter。"""

    scopes: list[tuple[dict[str, tuple[str, str]], bool]]

    async def authoritative_candidates(
        self, identities: list[MetadataSemanticHit]
    ) -> list[object]:
        """仅返回当前指纹的第一个候选。"""
        return [identities[0]] if identities else []

    async def resolve_value_scope(
        self, column_ids: set[str]
    ) -> tuple[dict[str, tuple[str, str]], bool]:
        """按调用顺序返回权威范围。"""
        del column_ids
        return self.scopes.pop(0)

    async def authoritative_value_candidates(
        self,
        projections: list[MetadataValueProjection],
        scope: dict[str, tuple[str, str]],
    ) -> list[MetadataValueCandidate]:
        """把匹配范围的投影转换为候选。"""
        return [
            MetadataValueCandidate(
                column_id=item.column_id,
                table_id=item.table_id,
                value=item.value_text,
                frequency=item.frequency,
            )
            for item in projections
            if scope.get(item.column_id)
            == (item.table_id, item.schema_fingerprint)
        ]


async def test_search_uses_derived_candidates_then_authoritative_readback() -> None:
    """语义搜索必须只把派生身份交给 Meta 权威回读。"""
    hit = MetadataSemanticHit(
        kind=MetadataObjectKind.COLUMN,
        object_id="column-1",
        schema_fingerprint="f" * 64,
        score=0.9,
        matched_text="订单状态",
    )

    class SemanticSearch:
        """返回一个派生索引身份。"""

        async def search(
            self,
            query: str,
            kinds: set[MetadataObjectKind] | None,
            limit: int,
        ) -> list[MetadataSemanticHit]:
            """验证用例传递有界预算。"""
            assert (query, kinds, limit) == ("订单", None, 20)
            return [hit]

    reader = _SearchReader([])
    service = _build_search_service(
        reader=reader,
        semantic_index=SemanticSearch(),
        value_index=_Unused(),
        search_limit=20,
    )

    assert await service.search_metadata("订单") == [hit]


async def test_value_search_rejects_changed_visible_generation() -> None:
    """查询期间可见代次改变时不得返回旧 ES 命中。"""
    scope = {"column-1": ("table-1", "schema-1")}
    projection = MetadataValueProjection(
        column_id="column-1",
        table_id="table-1",
        value_text="华东",
        value_keyword="华东",
        frequency=3,
        refresh_version="v1",
        schema_fingerprint="schema-1",
    )

    class ValueSearch:
        """模拟搜索期间出现新可见代次。"""

        def __init__(self) -> None:
            self.reads = 0

        async def current_refresh_versions(
            self, table_ids: set[str]
        ) -> dict[str, frozenset[str]]:
            assert table_ids == {"table-1"}
            self.reads += 1
            versions = {"v1"} if self.reads == 1 else {"v1", "v2"}
            return {"table-1": frozenset(versions)}

        async def search(
            self, query: str, column_ids: set[str], limit: int
        ) -> list[MetadataValueProjection]:
            assert (query, column_ids, limit) == ("华东", {"column-1"}, 20)
            return [projection]

    service = _build_search_service(
        reader=_SearchReader([(scope, True), (scope, True), (scope, True)]),
        semantic_index=_Unused(),
        value_index=ValueSearch(),
        search_limit=20,
    )

    result = await service.search_values("华东", {"column-1"})

    assert result.values == []
    assert result.complete is False


async def test_rebuild_persists_recovery_before_destructive_work() -> None:
    """Reset 只持久化两个恢复阶段，不直接删除远程索引。"""
    enqueued: list[object] = []

    class RebuildStore:
        """记录重建 desired state。"""

        async def enqueue(self, desired: object) -> None:
            enqueued.extend(desired)  # type: ignore[arg-type]

    rebuilder = _build_rebuilder(
        work_store=RebuildStore(),
        reader=_Unused(),
        semantic_index=_Unused(),
        value_index=_Unused(),
        projection_version="v1",
        es_index="metadata-values",
        qdrant_collection="metadata-semantic",
    )

    await rebuilder.reset_indexes(
        confirmed_es_index="metadata-values",
        confirmed_qdrant_collection="metadata-semantic",
    )

    assert len(enqueued) == 2
    assert {item.target for item in enqueued} == set(MetadataIndexTarget)  # type: ignore[attr-defined]
    assert {item.operation for item in enqueued} == {  # type: ignore[attr-defined]
        MetadataIndexOperation.REBUILD
    }
