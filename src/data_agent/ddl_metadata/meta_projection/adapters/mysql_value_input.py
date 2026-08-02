"""绑定调用方 MySQL Session 的 Meta Projection 值输入适配器。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.data_sync.models import DesiredSyncTable
from data_agent.ddl_metadata.meta_projection.application.value_input import (
    MaterializedRowsChanged,
    MaterializedTableRef,
    PreparedValueProjection,
)
from data_agent.ddl_metadata.meta_projection.desired import enqueue_value_refresh
from data_agent.ddl_metadata.meta_projection.value_refresh import (
    FrequencyMutationState,
    apply_frequency_row_changes,
    prepare_frequency_mutation,
)


class MySQLValueProjectionParticipant:
    """以调用方同一个 Session 参与物化行事务。"""

    def __init__(self, session: AsyncSession, desired: DesiredSyncTable) -> None:
        """捕获当前事务和仅供外层适配使用的 Data Sync 结构。"""
        self._session = session
        self._desired = desired

    async def prepare(self, table: MaterializedTableRef) -> PreparedValueProjection:
        """在 DW DML 前锁定当前来源适用的频次状态。"""
        _validate_table_ref(table, self._desired)
        states = await prepare_frequency_mutation(self._session, self._desired)
        return _MySQLPreparedValueProjection(
            session=self._session,
            desired=self._desired,
            table=table,
            states=states,
        )


@dataclass(frozen=True)
class _MySQLPreparedValueProjection:
    """保存同一事务中 prepare 阶段取得的私有频次状态。"""

    session: AsyncSession
    desired: DesiredSyncTable
    table: MaterializedTableRef
    states: Sequence[FrequencyMutationState]

    @property
    def needs_before_rows(self) -> bool:
        """仅在存在可增量维护的稳定频次代次时读取 before 镜像。"""
        return bool(self.states)

    async def apply(self, change: MaterializedRowsChanged) -> None:
        """在捕获的 Session 中原子写入频次差量与 refresh desired。"""
        if change.table != self.table:
            raise ValueError("物化行变化与 prepare 阶段的表引用不一致")
        # 步骤一：先在 prepare 已锁定的代次上应用精确 before/after 差量。
        await apply_frequency_row_changes(
            self.session,
            self.states,
            change.before_rows,
            change.after_rows,
        )
        # 步骤二：随后使用同一个 Session 合并 refresh desired，由调用方统一提交。
        await enqueue_value_refresh(self.session, self.desired, change.checkpoint)


def _validate_table_ref(
    table: MaterializedTableRef,
    desired: DesiredSyncTable,
) -> None:
    """拒绝把一个 transaction-scoped adapter 用于另一张物化表。"""
    expected = (
        desired.desired_hash(),
        desired.source,
        desired.source_schema,
        desired.source_table,
        desired.target_table,
        tuple(desired.primary_key),
    )
    actual = (
        table.table_id,
        table.source_id,
        table.source_schema,
        table.source_table,
        table.target_table,
        table.primary_key,
    )
    if actual != expected:
        raise ValueError("物化表引用与当前事务参与者绑定的同步结构不一致")
