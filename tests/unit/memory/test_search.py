"""记忆搜索服务的单元契约测试。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from types import TracebackType

import pytest

from data_agent.memory.application import search as search_module
from data_agent.memory.application.search import MemorySearchService
from data_agent.memory.domain.payloads import memory_content_hash
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
from data_agent.settings import app_config
from tests.helpers.checks import check_equal


class _FakeSession:
    """测试用异步 Session 占位符。"""


class _FakeSessionContext:
    """模拟 MySQL 会话上下文。"""

    async def __aenter__(self) -> _FakeSession:
        """返回测试 Session。"""
        return _FakeSession()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """不处理上下文异常。"""


class _FakeMySQLDatabase:
    """提供可替换的 MySQL 会话工厂。"""

    @classmethod
    def session(cls) -> _FakeSessionContext:
        """创建测试会话上下文。"""
        return _FakeSessionContext()


class _FakeTEIClient:
    """返回固定查询向量。"""

    async def aembed_query(self, query: str) -> list[float]:
        """忽略查询并返回固定向量。"""
        return [0.1, 0.2]


class _FakeClientProvider:
    """模拟基础设施客户端提供者。"""

    @classmethod
    def get_client(cls) -> object:
        """返回可传给索引封装的客户端。"""
        return _FakeTEIClient()


class _FakeElasticsearchIndex:
    """模拟 Elasticsearch 召回。"""

    def __init__(self, client: object) -> None:
        """记录初始化客户端。"""
        self._client = client

    async def search(
        self,
        query: str,
        source: str,
        categories: set[str] | None,
        limit: int,
        *,
        user_id: str | None,
    ) -> list[str]:
        """不返回派生索引候选。"""
        return []


class _FakeQdrantIndex(_FakeElasticsearchIndex):
    """模拟 Qdrant 召回。"""

    async def search(
        self,
        vector: Sequence[float],
        source: str,
        categories: set[str] | None,
        limit: int,
        *,
        user_id: str | None,
    ) -> list[str]:
        """不返回派生索引候选。"""
        return []


class _FakeMemoryIndexOutboxRepository:
    """模拟索引 outbox 仓储。"""

    def __init__(self, session: _FakeSession) -> None:
        """记录测试 Session。"""
        self._session = session

    async def pending_targets(
        self,
        candidate_uids: set[str],
    ) -> dict[str, set[MemoryIndexTarget]]:
        """返回无待处理投影目标。"""
        return {}


@pytest.mark.asyncio
async def test_search_returns_items_when_record_access_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """访问统计失败不能中断已经完成权威回查的搜索结果。"""
    content = SemanticDecisionContent(
        table=SemanticTable(
            table_id="orders",
            role=TableRole.FACT,
            description="订单事实表",
            confidence=0.9,
        )
    )
    detail = MemoryDetail(
        uid="memory-1",
        source="dw",
        category=BuiltinMemoryCategory.DDL_SEMANTIC.value,
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
        content_version=app_config.memory.content_version,
        projection_version=app_config.memory.projection_version,
        created_at=datetime.now(UTC).replace(tzinfo=None),
        updated_at=datetime.now(UTC).replace(tzinfo=None),
    )

    class FakeMemoryRepository:
        """模拟权威记忆仓储，并让访问统计失败。"""

        def __init__(self, session: _FakeSession) -> None:
            """记录测试 Session。"""
            self._session = session

        async def find_exact_query(
            self,
            source: str,
            query: str,
            categories: set[str] | None,
            *,
            user_id: str | None,
            limit: int,
        ) -> list[str]:
            """返回基线精确候选。"""
            return [detail.uid]

        async def get_many_active(
            self,
            uids: list[str],
            *,
            user_id: str | None,
        ) -> list[object]:
            """返回 MySQL 权威复核结果。"""

            class Row:
                """模拟仓储返回对象。"""

                def __init__(self, memory_detail: MemoryDetail) -> None:
                    """保存详情。"""
                    self.detail = memory_detail

            return [Row(detail)]

        async def record_access(
            self,
            uids: set[str],
            *,
            source: str,
            user_id: str | None,
        ) -> None:
            """模拟非关键访问统计写入失败。"""
            raise TimeoutError("lock wait timeout")

    monkeypatch.setattr(search_module, "MySQLDatabase", _FakeMySQLDatabase)
    monkeypatch.setattr(search_module, "MemoryRepository", FakeMemoryRepository)
    monkeypatch.setattr(
        search_module,
        "MemoryIndexOutboxRepository",
        _FakeMemoryIndexOutboxRepository,
    )
    monkeypatch.setattr(search_module, "ElasticsearchClient", _FakeClientProvider)
    monkeypatch.setattr(search_module, "QdrantClient", _FakeClientProvider)
    monkeypatch.setattr(search_module, "TEIEmbeddingClient", _FakeClientProvider)
    monkeypatch.setattr(
        search_module, "MemoryElasticsearchIndex", _FakeElasticsearchIndex
    )
    monkeypatch.setattr(search_module, "MemoryQdrantIndex", _FakeQdrantIndex)

    response = await MemorySearchService().search("订单", "dw")

    check_equal("访问统计失败后仍返回搜索结果数量", len(response.items), 1)
    check_equal(
        "访问统计失败后返回原始记忆 UID", response.items[0].memory.uid, detail.uid
    )
