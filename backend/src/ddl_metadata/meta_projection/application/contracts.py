"""Meta Projection 运行时端口。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import AsyncContextManager, Protocol

from ddl_metadata.meta_projection.models import (
    ClaimedMetadataIndexWork,
    MetadataCandidate,
    MetadataIndexDesired,
    MetadataIndexTarget,
    MetadataObjectKind,
    MetadataSemanticHit,
    MetadataSemanticProjection,
    MetadataValueCandidate,
    MetadataValueProjection,
)


class ProjectionNotReadyError(RuntimeError):
    """权威 DW 投影尚未物化，任务应无损延后。"""


class ValueRefreshPersistenceError(RuntimeError):
    """字段值刷新本地持久化失败，任务应无损延后。"""


class RebuildProjectionError(RuntimeError):
    """后端重建后的权威扫描失败，任务应无损延后。"""


class ProjectionWorkStore(Protocol):
    """领取、保护并结算投影 desired state 的持久化端口。"""

    async def claim(self, limit: int) -> list[ClaimedMetadataIndexWork]:
        """领取至多指定数量的工作项。"""
        ...

    def authority(self, item: ClaimedMetadataIndexWork) -> AsyncContextManager[bool]:
        """在投影锁内续租并返回完整 desired identity 是否仍权威。"""
        ...

    async def enqueue(self, desired: Sequence[MetadataIndexDesired]) -> None:
        """原子合并一批投影 desired state。"""
        ...

    async def acknowledge(self, item: ClaimedMetadataIndexWork) -> bool:
        """按完整 desired identity 确认成功。"""
        ...

    async def restore_reconciliation(self, item: ClaimedMetadataIndexWork) -> bool:
        """为迟到远程写入恢复当前 desired state 的可领取性。"""
        ...

    async def defer(self, item: ClaimedMetadataIndexWork) -> bool:
        """无损延后本地尚未就绪或持久化失败的工作。"""
        ...

    async def backoff(
        self,
        item: ClaimedMetadataIndexWork,
        error_type: str,
    ) -> bool:
        """为远程失败增加有界退避与失败次数。"""
        ...

    async def dead_letter_count(self) -> int:
        """返回达到最大失败次数的工作项数量。"""
        ...


class ProjectionReader(Protocol):
    """从 Meta 权威状态构造投影与回查候选的读取端口。"""

    async def semantic_projection(
        self,
        kind: MetadataObjectKind,
        object_id: str,
    ) -> MetadataSemanticProjection | None:
        """读取一个当前语义投影。"""
        ...

    async def semantic_identities(self) -> list[tuple[MetadataObjectKind, str]]:
        """读取当前全部语义对象身份。"""
        ...

    async def eligible_table_ids(self) -> set[str]:
        """读取当前需要字段值投影的表身份。"""
        ...

    async def schema_is_authoritative(
        self, source: str, schema_fingerprint: str
    ) -> bool:
        """确认完整物理模式指纹仍是该来源的权威快照。"""
        ...

    async def authoritative_candidates(
        self,
        identities: list[MetadataSemanticHit],
        *,
        table_ids: set[str] | None = None,
        column_ids: set[str] | None = None,
    ) -> list[MetadataCandidate]:
        """按派生索引顺序回读当前 Meta 候选。"""
        ...

    async def resolve_value_scope(
        self,
        column_ids: set[str],
    ) -> tuple[dict[str, tuple[str, str]], bool]:
        """解析字段值查询的当前权威范围与完整性。"""
        ...

    async def authoritative_value_candidates(
        self,
        projections: list[MetadataValueProjection],
        scope: dict[str, tuple[str, str]],
    ) -> list[MetadataValueCandidate]:
        """拒绝越界或过期的字段值命中。"""
        ...


class SemanticIndex(Protocol):
    """隐藏 Qdrant 与 embedding 细节的语义投影端口。"""

    async def setup(self) -> None:
        """创建或严格校验语义索引。"""
        ...

    async def recreate(self) -> None:
        """幂等重建项目语义索引。"""
        ...

    async def upsert(self, projection: MetadataSemanticProjection) -> None:
        """写入一个语义投影。"""
        ...

    async def delete(self, kind: MetadataObjectKind, object_id: str) -> None:
        """删除一个语义投影。"""
        ...

    async def search(
        self,
        query: str,
        kinds: set[MetadataObjectKind] | None,
        limit: int,
        *,
        table_ids: set[str] | None = None,
        column_ids: set[str] | None = None,
    ) -> list[MetadataSemanticHit]:
        """返回有界语义候选身份。"""
        ...


class ValueIndex(Protocol):
    """字段值派生索引端口。"""

    async def setup(self) -> None:
        """创建或严格校验字段值索引。"""
        ...

    async def recreate(self) -> None:
        """幂等重建项目字段值索引。"""
        ...

    async def search(
        self,
        query: str,
        column_ids: set[str],
        limit: int,
    ) -> list[MetadataValueProjection]:
        """返回有界字段值候选投影。"""
        ...

    async def current_refresh_versions(
        self,
        table_ids: set[str],
    ) -> dict[str, frozenset[str]]:
        """读取各表当前所有可见刷新代次。"""
        ...


class ValueRefreshRunner(Protocol):
    """执行一个有界、可恢复字段值刷新单元的端口。"""

    async def run_next_unit(self, item: ClaimedMetadataIndexWork) -> bool:
        """推进一个持久化工作单元并返回该单元是否成功完成。"""
        ...


class ProjectionRebuilder(Protocol):
    """执行单个派生索引持久化重建阶段的端口。"""

    async def rebuild_target(self, target: MetadataIndexTarget) -> None:
        """重建一个目标并重新投递其当前权威对象。"""
        ...
