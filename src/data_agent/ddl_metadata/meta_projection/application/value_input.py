"""Meta Projection 接收物化行变化的中立应用契约。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class MaterializedTableRef:
    """不依赖 Data Sync 持久化模型的物化表引用。"""

    table_id: str
    source_id: str
    source_schema: str
    source_table: str
    target_table: str
    primary_key: tuple[str, ...]


@dataclass(frozen=True)
class MaterializedRowsChanged:
    """调用方事务内已完成 DML 的 before/after 行变化。"""

    table: MaterializedTableRef
    before_rows: tuple[Mapping[str, object], ...]
    after_rows: tuple[Mapping[str, object], ...]
    checkpoint: Mapping[str, object]


class PreparedValueProjection(Protocol):
    """在 DW DML 前完成锁定、等待调用方提交行变化的中立令牌。"""

    @property
    def needs_before_rows(self) -> bool:
        """返回当前变化是否需要读取加锁的 DW before 镜像。"""
        ...

    async def apply(self, change: MaterializedRowsChanged) -> None:
        """在绑定的调用方事务中应用频次差量并合并刷新期望。"""
        ...


class ValueProjectionParticipant(Protocol):
    """加入调用方当前事务的 Meta Projection 值输入参与者。"""

    async def prepare(self, table: MaterializedTableRef) -> PreparedValueProjection:
        """在任何 DW DML 前锁定适用状态并返回不泄漏实现的令牌。"""
        ...
