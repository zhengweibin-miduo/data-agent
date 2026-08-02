"""Long-term Memory 投影调度应用 seam 测试。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from data_agent.memory.application.contracts import (
    MemoryProjectionDispatchConfig,
    PreparedMemoryProjection,
)
from data_agent.memory.application.index_dispatcher import MemoryIndexDispatcher
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


def _projection(
    uid: str,
    status: MemoryStatus = MemoryStatus.ACTIVE,
) -> MemoryProjection:
    """构造可观察的权威投影。"""
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
        content_version="v1",
        projection_version="v1",
        created_at=moment,
        updated_at=moment,
    )


def _item(uid: str, target: MemoryIndexTarget) -> MemoryOutboxItem:
    """构造一个已领取的目标期望状态。"""
    return MemoryOutboxItem(
        memory_uid=uid,
        target=target,
        operation=MemoryIndexOperation.UPSERT,
        projection_version="v1",
        attempts=0,
        lease_token="lease-1",
    )


class InMemoryProjectionWorkStore:
    """以可观察持久状态实现调度 work port。"""

    def __init__(
        self,
        items: Sequence[MemoryOutboxItem],
        projections: dict[str, MemoryProjection | None],
    ) -> None:
        """初始化领取队列与权威投影。"""
        self.items = list(items)
        self.projections = projections
        self.lost: set[tuple[str, MemoryIndexTarget]] = set()
        self.superseded: set[tuple[str, MemoryIndexTarget]] = set()
        self.acknowledged: set[tuple[str, MemoryIndexTarget]] = set()
        self.pending: set[tuple[str, MemoryIndexTarget]] = set()
        self.retried: dict[tuple[str, MemoryIndexTarget], str] = {}
        self.dead_letters = 0

    async def claim(self, limit: int) -> list[MemoryOutboxItem]:
        """领取有界批次。"""
        return self.items[:limit]

    async def prepare(self, item: MemoryOutboxItem) -> PreparedMemoryProjection:
        """复核领取代次并返回权威投影。"""
        key = (item.memory_uid, item.target)
        return PreparedMemoryProjection(
            authority_held=key not in self.lost,
            projection=self.projections.get(item.memory_uid),
        )

    async def settle_success(
        self,
        item: MemoryOutboxItem,
        *,
        content_hash: str | None,
    ) -> bool:
        """确认当前 authority，或登记可重放的收敛请求。"""
        del content_hash
        key = (item.memory_uid, item.target)
        if key in self.superseded:
            self.pending.add(key)
            return False
        self.acknowledged.add(key)
        return True

    async def settle_failure(
        self,
        item: MemoryOutboxItem,
        *,
        error_type: str,
        max_backoff_seconds: int,
    ) -> None:
        """记录目标独立退避。"""
        del max_backoff_seconds
        self.retried[(item.memory_uid, item.target)] = error_type

    async def dead_letter_count(self) -> int:
        """返回停止重试的期望状态数量。"""
        return self.dead_letters


class InMemoryProjectionIndex:
    """记录单个派生目标的最终可观察内容。"""

    def __init__(self, *, fail: bool = False) -> None:
        """初始化派生文档集合。"""
        self.documents: dict[str, MemoryProjection] = {}
        self.fail = fail

    async def apply(
        self,
        memory_uid: str,
        projection: MemoryProjection | None,
    ) -> None:
        """幂等写入或删除一个派生文档。"""
        if self.fail:
            raise TimeoutError("projection timeout")
        if projection is None:
            self.documents.pop(memory_uid, None)
        else:
            self.documents[memory_uid] = projection


def _dispatcher(
    work: InMemoryProjectionWorkStore,
    *,
    elasticsearch: InMemoryProjectionIndex | None = None,
    qdrant: InMemoryProjectionIndex | None = None,
) -> MemoryIndexDispatcher:
    """构造只依赖公开 ports 的调度用例。"""
    return MemoryIndexDispatcher(
        work,
        {
            MemoryIndexTarget.ELASTICSEARCH: elasticsearch
            or InMemoryProjectionIndex(),
            MemoryIndexTarget.QDRANT: qdrant or InMemoryProjectionIndex(),
        },
        MemoryProjectionDispatchConfig(batch_size=10, max_backoff_seconds=300),
    )


async def test_dispatch_claims_writes_and_settles_active_authority() -> None:
    """活动权威投影经领取、远程写入后独立确认。"""
    item = _item("memory-1", MemoryIndexTarget.ELASTICSEARCH)
    projection = _projection(item.memory_uid)
    work = InMemoryProjectionWorkStore([item], {item.memory_uid: projection})
    index = InMemoryProjectionIndex()

    processed = await _dispatcher(work, elasticsearch=index).dispatch()

    assert processed == 1
    assert index.documents == {item.memory_uid: projection}
    assert work.acknowledged == {(item.memory_uid, item.target)}


@pytest.mark.parametrize(
    ("operation", "projection"),
    [
        (MemoryIndexOperation.DELETE, _projection("memory-2")),
        (MemoryIndexOperation.UPSERT, _projection("memory-2", MemoryStatus.DELETED)),
        (MemoryIndexOperation.UPSERT, None),
    ],
)
async def test_dispatch_converges_delete_and_non_active_authority(
    operation: MemoryIndexOperation,
    projection: MemoryProjection | None,
) -> None:
    """删除、非活动或缺失 authority 均收敛为派生删除。"""
    item = _item("memory-2", MemoryIndexTarget.ELASTICSEARCH).model_copy(
        update={"operation": operation}
    )
    work = InMemoryProjectionWorkStore([item], {item.memory_uid: projection})
    index = InMemoryProjectionIndex()
    index.documents[item.memory_uid] = _projection(item.memory_uid)

    assert await _dispatcher(work, elasticsearch=index).dispatch() == 1
    assert item.memory_uid not in index.documents


async def test_dispatch_does_not_write_or_settle_after_authority_loss() -> None:
    """领取 authority 丢失后不执行远程写入、确认或失败退避。"""
    item = _item("memory-3", MemoryIndexTarget.ELASTICSEARCH)
    work = InMemoryProjectionWorkStore(
        [item],
        {item.memory_uid: _projection(item.memory_uid)},
    )
    work.lost.add((item.memory_uid, item.target))
    index = InMemoryProjectionIndex()

    assert await _dispatcher(work, elasticsearch=index).dispatch() == 0
    assert index.documents == {}
    assert work.acknowledged == set()
    assert work.retried == {}


async def test_dispatch_isolates_failure_to_one_projection_target() -> None:
    """单目标失败仅退避自身，另一目标仍可收敛。"""
    items = [_item("memory-4", target) for target in MemoryIndexTarget]
    projection = _projection("memory-4")
    work = InMemoryProjectionWorkStore(items, {"memory-4": projection})
    es = InMemoryProjectionIndex(fail=True)
    qdrant = InMemoryProjectionIndex()

    processed = await _dispatcher(work, elasticsearch=es, qdrant=qdrant).dispatch()

    assert processed == 1
    assert work.retried == {
        ("memory-4", MemoryIndexTarget.ELASTICSEARCH): "TimeoutError"
    }
    assert qdrant.documents == {"memory-4": projection}
    assert work.acknowledged == {("memory-4", MemoryIndexTarget.QDRANT)}


async def test_dispatch_registers_durable_convergence_when_authority_changes() -> None:
    """远程写期间 authority 变化后留下可重放期望状态，不确认迟到写入。"""
    item = _item("memory-5", MemoryIndexTarget.ELASTICSEARCH)
    projection = _projection(item.memory_uid)
    work = InMemoryProjectionWorkStore([item], {item.memory_uid: projection})
    work.superseded.add((item.memory_uid, item.target))
    index = InMemoryProjectionIndex()

    assert await _dispatcher(work, elasticsearch=index).dispatch() == 0
    assert index.documents == {item.memory_uid: projection}
    assert work.acknowledged == set()
    assert work.pending == {(item.memory_uid, item.target)}


async def test_report_dead_letters_exposes_stopped_work() -> None:
    """死信报告公开达到失败上限、已停止领取的积压数。"""
    work = InMemoryProjectionWorkStore([], {})
    work.dead_letters = 3

    assert await _dispatcher(work).report_dead_letters() == 3
