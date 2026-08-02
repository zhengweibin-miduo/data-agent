"""Long-term Memory 应用用例的端口与显式配置。"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from data_agent.models.memory import (
    MemoryCandidate,
    MemoryContent,
    MemoryDetail,
    MemoryHistoryPage,
    MemoryIndexTarget,
    MemoryOutboxItem,
    MemoryProjection,
)


@dataclass(frozen=True)
class StoredMemory:
    """应用层可见的权威记忆及其持久化标识。"""

    id: int
    detail: MemoryDetail


@dataclass(frozen=True)
class MemoryServiceConfig:
    """记忆变更用例所需的显式版本配置。"""

    projection_version: str


@dataclass(frozen=True)
class MemorySearchConfig:
    """混合检索用例的预算与排序配置。"""

    search_limit: int
    lexical_top_k: int
    vector_top_k: int
    timeout_seconds: float
    rrf_constant: int


@dataclass(frozen=True)
class MemoryProjectionDispatchConfig:
    """投影调度批次与失败退避预算。"""

    batch_size: int
    max_backoff_seconds: int


@dataclass(frozen=True)
class PreparedMemoryProjection:
    """短事务复核后的领取 authority 与当前权威投影。"""

    authority_held: bool
    projection: MemoryProjection | None


class MemoryMutationLeaseProvider(Protocol):
    """为 DDL 记忆变更提供来源级互斥租约。"""

    def mutation_lease(self, source: str) -> AbstractAsyncContextManager[None]:
        """获取指定来源的异步互斥上下文。"""
        ...


class MemoryStore(Protocol):
    """封装权威记忆读写及其短事务语义。"""

    async def get(self, uid: str, *, user_id: str | None) -> StoredMemory | None:
        """按租户边界读取权威记忆。"""
        ...

    async def history(
        self,
        uid: str,
        *,
        user_id: str | None,
        offset: int,
        limit: int,
    ) -> MemoryHistoryPage | None:
        """读取有界只追加历史。"""
        ...

    async def replace(
        self,
        current_uid: str,
        candidate: MemoryCandidate,
        content: MemoryContent,
        *,
        user_id: str | None,
        expected_version: int,
    ) -> int:
        """复核当前版本并原子替换，返回最新事件编号。"""
        ...

    async def delete(
        self,
        uid: str,
        *,
        user_id: str | None,
        expected_version: int,
    ) -> None:
        """复核当前版本并执行可审计软删除。"""
        ...


class MemorySearchStore(Protocol):
    """封装检索所需的 MySQL 权威操作。"""

    async def find_exact(
        self,
        source: str,
        query: str,
        categories: set[str] | None,
        *,
        user_id: str | None,
        limit: int,
    ) -> list[str]:
        """返回 MySQL 精确基线候选。"""
        ...

    async def load_authority(
        self, uids: set[str], *, user_id: str | None
    ) -> list[MemoryDetail]:
        """批量读取同租户活动权威记忆。"""
        ...

    async def pending_targets(
        self, uids: set[str]
    ) -> dict[str, set[MemoryIndexTarget]]:
        """返回每个候选尚未收敛的投影目标。"""
        ...

    async def record_access(
        self, uids: set[str], *, source: str, user_id: str | None
    ) -> None:
        """尽力记录权威记忆访问统计。"""
        ...


class LexicalMemoryIndex(Protocol):
    """提供租户隔离的词法候选 UID。"""

    async def search(
        self,
        query: str,
        source: str,
        categories: set[str] | None,
        limit: int,
        *,
        user_id: str | None,
    ) -> list[str]:
        """搜索词法候选 UID。"""
        ...


class VectorMemoryIndex(Protocol):
    """提供租户隔离的向量候选 UID。"""

    async def search(
        self,
        vector: Sequence[float],
        source: str,
        categories: set[str] | None,
        limit: int,
        *,
        user_id: str | None,
    ) -> list[str]:
        """搜索向量候选 UID。"""
        ...


class EmbeddingProvider(Protocol):
    """把检索文本转换为查询向量。"""

    async def embed_query(self, query: str) -> list[float]:
        """生成查询向量。"""
        ...


class MemoryProjectionWorkStore(Protocol):
    """封装投影领取、authority 复核、结算与死信的短事务。"""

    async def claim(self, limit: int) -> list[MemoryOutboxItem]:
        """领取一个有界期望状态批次。"""
        ...

    async def prepare(self, item: MemoryOutboxItem) -> PreparedMemoryProjection:
        """续租并在同一短事务读取当前权威投影。"""
        ...

    async def settle_success(
        self,
        item: MemoryOutboxItem,
        *,
        content_hash: str | None,
    ) -> bool:
        """按完整 authority 确认，失败时原子登记持久收敛请求。"""
        ...

    async def settle_failure(
        self,
        item: MemoryOutboxItem,
        *,
        error_type: str,
        max_backoff_seconds: int,
    ) -> None:
        """仅为当前目标登记有界失败退避。"""
        ...

    async def dead_letter_count(self) -> int:
        """返回达到失败上限且停止领取的期望状态数量。"""
        ...


class MemoryProjectionIndex(Protocol):
    """把一个目标收敛到当前权威投影或删除状态。"""

    async def apply(
        self,
        memory_uid: str,
        projection: MemoryProjection | None,
    ) -> None:
        """幂等写入或删除一个派生目标文档。"""
        ...


class MemoryMaintenanceStore(Protocol):
    """封装 Long-term Memory 到期与安全物理清理事务。"""

    async def expire_due(self) -> int:
        """失效到期权威记忆并登记删除期望状态。"""
        ...

    async def purge_ready_user_memories(self) -> int:
        """物理清理已完成双目标删除的用户记忆。"""
        ...
