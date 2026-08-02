"""Long-term Memory 搜索 public seam 契约测试。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from data_agent.memory.application.contracts import MemorySearchConfig
from data_agent.memory.application.search import MemorySearchService
from data_agent.memory.domain.payloads import memory_content_hash
from data_agent.memory.versions import category_content_version
from data_agent.models.memory import (
    BuiltinMemoryCategory,
    MemoryDetail,
    MemoryIndexTarget,
    MemoryLifecyclePolicy,
    MemoryStatus,
    MemoryTrust,
    SemanticDecisionContent,
)
from data_agent.models.semantic import SemanticTable, TableRole


def _detail(uid: str = "memory-1") -> MemoryDetail:
    """构造一条当前且可验证的权威记忆。"""
    content = SemanticDecisionContent(
        table=SemanticTable(
            table_id="orders",
            role=TableRole.FACT,
            description="订单事实表",
            confidence=0.9,
        )
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    category = BuiltinMemoryCategory.DDL_SEMANTIC.value
    return MemoryDetail(
        uid=uid,
        source="dw",
        category=category,
        memory_key="orders",
        content_schema="semantic.v1",
        memory_text="订单事实表",
        content=content,
        content_hash=memory_content_hash(content),
        trust=MemoryTrust.MODEL_VALIDATED,
        status=MemoryStatus.ACTIVE,
        importance_score=0.5,
        lifecycle_policy=MemoryLifecyclePolicy.FINGERPRINT_BOUND,
        record_version=1,
        access_count=0,
        content_version=category_content_version(category),
        projection_version="older-projection",
        created_at=now,
        updated_at=now,
    )


class _SearchStore:
    """搜索 seam 的内存权威 store。"""

    def __init__(self, memories: list[MemoryDetail]) -> None:
        """保存权威记录及可配置的投影状态。"""
        self.memories = memories
        self.pending: dict[str, set[MemoryIndexTarget]] = {}
        self.access_error: BaseException | None = None

    async def find_exact(
        self,
        source: str,
        query: str,
        categories: set[str] | None,
        *,
        user_id: str | None,
        limit: int,
    ) -> list[str]:
        """返回第一条权威记忆作为精确基线。"""
        del source, query, categories, user_id, limit
        return [self.memories[0].uid] if self.memories else []

    async def load_authority(
        self, uids: set[str], *, user_id: str | None
    ) -> list[MemoryDetail]:
        """返回由输入 UID 选中的权威记录。"""
        del user_id
        return [memory for memory in self.memories if memory.uid in uids]

    async def pending_targets(
        self, uids: set[str]
    ) -> dict[str, set[MemoryIndexTarget]]:
        """返回测试配置的未收敛目标。"""
        del uids
        return self.pending

    async def record_access(
        self, uids: set[str], *, source: str, user_id: str | None
    ) -> None:
        """按需模拟非关键访问统计失败。"""
        del uids, source, user_id
        if self.access_error is not None:
            raise self.access_error


class _LexicalIndex:
    """可配置的词法索引边界。"""

    def __init__(self, result: list[str] | BaseException) -> None:
        """保存候选或边界异常。"""
        self.result = result

    async def search(
        self,
        query: str,
        source: str,
        categories: set[str] | None,
        limit: int,
        *,
        user_id: str | None,
    ) -> list[str]:
        """返回候选或抛出远程失败。"""
        del query, source, categories, limit, user_id
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _VectorIndex:
    """可配置的向量索引边界。"""

    def __init__(self, result: list[str] | BaseException) -> None:
        """保存候选或边界异常。"""
        self.result = result

    async def search(
        self,
        vector: Sequence[float],
        source: str,
        categories: set[str] | None,
        limit: int,
        *,
        user_id: str | None,
    ) -> list[str]:
        """返回候选或抛出远程失败。"""
        del vector, source, categories, limit, user_id
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _Embeddings:
    """返回固定查询向量的边界替身。"""

    async def embed_query(self, query: str) -> list[float]:
        """返回固定二维向量。"""
        del query
        return [0.1, 0.2]


def _service(
    store: _SearchStore,
    lexical: list[str] | BaseException | None = None,
    vector: list[str] | BaseException | None = None,
) -> MemorySearchService:
    """从 public ports 构造搜索用例。"""
    return MemorySearchService(
        store,
        _LexicalIndex([] if lexical is None else lexical),
        _VectorIndex([] if vector is None else vector),
        _Embeddings(),
        MemorySearchConfig(10, 20, 20, 1.0, 60),
    )


@pytest.mark.asyncio
async def test_exact_baseline_survives_both_remote_failures() -> None:
    """两个远程路径失败时仍返回经权威复核的 MySQL 精确基线。"""
    detail = _detail()
    response = await _service(
        _SearchStore([detail]), TimeoutError(), ConnectionError()
    ).search("订单", "dw")

    assert [item.memory.uid for item in response.items] == [detail.uid]
    assert set(response.degraded_targets) == {
        MemoryIndexTarget.ELASTICSEARCH,
        MemoryIndexTarget.QDRANT,
    }


@pytest.mark.asyncio
async def test_pending_target_filters_only_its_remote_signal() -> None:
    """未收敛目标只移除自身排名信号，不移除另一目标候选。"""
    detail = _detail()
    store = _SearchStore([detail])
    store.pending = {detail.uid: {MemoryIndexTarget.ELASTICSEARCH}}
    response = await _service(store, [detail.uid], [detail.uid]).search(
        "其他查询", "dw", exact_uids=["missing"]
    )

    assert [item.memory.uid for item in response.items] == [detail.uid]
    assert response.items[0].signals == ["qdrant"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    [
        {"source": "other"},
        {"status": MemoryStatus.DELETED},
        {"content_hash": "0" * 64},
        {"content_version": "stale"},
        {"expires_at": datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)},
    ],
)
async def test_authority_guards_reject_stale_candidates(
    changed: dict[str, object],
) -> None:
    """来源、状态、哈希、内容版本与过期守卫拒绝陈旧候选。"""
    detail = _detail().model_copy(update=changed)
    response = await _service(_SearchStore([detail])).search("订单", "dw")

    assert response.items == []


@pytest.mark.asyncio
async def test_tenant_and_object_guards_reject_candidates() -> None:
    """租户不匹配和对象白名单不兼容时不返回候选。"""
    detail = _detail().model_copy(update={"user_id": "another-user"})
    tenant_response = await _service(_SearchStore([detail])).search(
        "订单", "dw", user_id="expected-user"
    )
    object_response = await _service(_SearchStore([_detail()])).search(
        "订单", "dw", allowed_object_ids={"customers"}
    )

    assert tenant_response.items == []
    assert object_response.items == []


@pytest.mark.asyncio
async def test_access_record_failure_does_not_hide_results() -> None:
    """访问统计失败不能撤销已完成的权威搜索结果。"""
    detail = _detail()
    store = _SearchStore([detail])
    store.access_error = TimeoutError("lock wait timeout")

    response = await _service(store).search("订单", "dw")

    assert [item.memory.uid for item in response.items] == [detail.uid]
