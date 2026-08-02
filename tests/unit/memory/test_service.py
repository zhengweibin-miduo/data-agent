"""Long-term Memory 管理 public seam 契约测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

import pytest

from data_agent.errors import DataAgentError
from data_agent.memory.application.contracts import (
    MemoryServiceConfig,
    StoredMemory,
)
from data_agent.memory.application.search import MemorySearchService
from data_agent.memory.application.service import MemoryService
from data_agent.memory.domain.payloads import memory_content_hash
from data_agent.memory.versions import category_content_version
from data_agent.models.memory import (
    BuiltinMemoryCategory,
    MemoryCandidate,
    MemoryContent,
    MemoryDetail,
    MemoryHistoryPage,
    MemoryLifecyclePolicy,
    MemoryStatus,
    MemoryTrust,
    SemanticDecisionContent,
)
from data_agent.models.semantic import SemanticTable, TableRole


def _content(description: str) -> SemanticDecisionContent:
    """构造保持对象身份不变的语义内容。"""
    return SemanticDecisionContent(
        table=SemanticTable(
            table_id="orders",
            role=TableRole.FACT,
            description=description,
            confidence=0.9,
        )
    )


def _stored() -> StoredMemory:
    """构造一条活动权威记忆。"""
    content = _content("订单事实表")
    category = BuiltinMemoryCategory.DDL_SEMANTIC.value
    now = datetime.now(UTC).replace(tzinfo=None)
    detail = MemoryDetail(
        uid="memory-1",
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
        projection_version="v1",
        created_at=now,
        updated_at=now,
    )
    return StoredMemory(1, detail)


class _MemoryStore:
    """管理用例 seam 的内存 store。"""

    def __init__(self, memory: StoredMemory | None) -> None:
        """保存可返回的权威记录。"""
        self.memory = memory
        self.deleted: str | None = None
        self.replacement: MemoryCandidate | None = None

    async def get(self, uid: str, *, user_id: str | None) -> StoredMemory | None:
        """返回配置的权威记录。"""
        del uid, user_id
        return self.memory

    async def history(
        self,
        uid: str,
        *,
        user_id: str | None,
        offset: int,
        limit: int,
    ) -> MemoryHistoryPage | None:
        """存在权威记录时返回空历史页。"""
        del uid, user_id
        if self.memory is None:
            return None
        return MemoryHistoryPage(items=[], offset=offset, limit=limit, has_more=False)

    async def replace(
        self,
        current_uid: str,
        candidate: MemoryCandidate,
        content: MemoryContent,
        *,
        user_id: str | None,
        expected_version: int,
    ) -> int:
        """保存替换候选并返回稳定事件编号。"""
        del current_uid, content, user_id, expected_version
        self.replacement = candidate
        return 42

    async def delete(
        self,
        uid: str,
        *,
        user_id: str | None,
        expected_version: int,
    ) -> None:
        """记录软删除目标。"""
        del user_id, expected_version
        self.deleted = uid


class _Leases:
    """提供无外部依赖的来源租约。"""

    @asynccontextmanager
    async def mutation_lease(self, source: str):  # type: ignore[no-untyped-def]
        """进入并退出测试租约。"""
        del source
        yield


def _service(store: _MemoryStore) -> MemoryService:
    """从 public ports 构造管理用例。"""
    unused_search = cast(MemorySearchService, object())
    return MemoryService(
        store,
        unused_search,
        _Leases(),
        MemoryServiceConfig(projection_version="v2"),
    )


@pytest.mark.asyncio
async def test_get_and_history_hide_missing_authority() -> None:
    """详情与历史都把缺失或租户不匹配投影为安全 404。"""
    service = _service(_MemoryStore(None))

    with pytest.raises(DataAgentError, match="记忆不存在") as get_error:
        await service.get("missing")
    with pytest.raises(DataAgentError, match="记忆不存在") as history_error:
        await service.history("missing", offset=0, limit=10)

    assert get_error.value.http_status == 404
    assert history_error.value.http_status == 404


@pytest.mark.asyncio
async def test_update_and_delete_use_authoritative_store_seam() -> None:
    """修正和软删除通过 store seam 返回现有公开响应。"""
    stored = _stored()
    store = _MemoryStore(stored)
    service = _service(store)

    update = await service.update(
        stored.detail.uid,
        _content("订单交易事实"),
        expected_version=1,
    )
    deleted = await service.delete(stored.detail.uid, expected_version=1)

    assert update.event_id == 42
    assert update.record_version == 2
    assert update.requires_reprocess is True
    assert store.replacement is not None
    assert store.replacement.projection_version == "v2"
    assert deleted.memory_uid == stored.detail.uid
    assert store.deleted == stored.detail.uid
