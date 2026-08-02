"""transaction-scoped Meta Projection 值输入公共 seam 测试。"""

from typing import cast
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.data_sync.models import DesiredColumn, DesiredSyncTable
from data_agent.ddl_metadata.meta_projection.adapters import mysql_value_input
from data_agent.ddl_metadata.meta_projection.adapters.mysql_value_input import (
    MySQLValueProjectionParticipant,
)
from data_agent.ddl_metadata.meta_projection.application.value_input import (
    MaterializedRowsChanged,
    MaterializedTableRef,
)


def _desired() -> DesiredSyncTable:
    """构造一个可物化的同步表结构。"""
    return DesiredSyncTable(
        source="local",
        source_schema="business",
        source_table="fact_order",
        target_table="fact_order",
        columns=[
            DesiredColumn(
                id="column-id",
                name="id",
                data_type="BIGINT",
                nullable=False,
            )
        ],
        primary_key=["id"],
        schema_fingerprint="a" * 64,
    )


async def test_mysql_participant_keeps_lock_delta_and_desired_on_one_session(
    monkeypatch,
) -> None:
    """prepare、频次差量和 refresh desired 必须使用调用方同一个 Session。"""
    session = cast(AsyncSession, AsyncMock())
    desired = _desired()
    table = MaterializedTableRef(
        table_id=desired.desired_hash(),
        source_id=desired.source,
        source_schema=desired.source_schema,
        source_table=desired.source_table,
        target_table=desired.target_table,
        primary_key=tuple(desired.primary_key),
    )
    observed: list[tuple[str, AsyncSession]] = []
    state = cast(mysql_value_input.FrequencyMutationState, object())

    async def prepare(actual_session, actual_desired):
        observed.append(("prepare", actual_session))
        assert actual_desired is desired
        return [state]

    async def apply(actual_session, states, before_rows, after_rows):
        observed.append(("delta", actual_session))
        assert states == [state]
        assert before_rows == ({"id": 1},)
        assert after_rows == ({"id": 2},)

    async def enqueue(actual_session, actual_desired, checkpoint):
        observed.append(("desired", actual_session))
        assert actual_desired is desired
        assert checkpoint == {"coordinate": {"position": 12}}

    monkeypatch.setattr(mysql_value_input, "prepare_frequency_mutation", prepare)
    monkeypatch.setattr(mysql_value_input, "apply_frequency_row_changes", apply)
    monkeypatch.setattr(mysql_value_input, "enqueue_value_refresh", enqueue)

    prepared = await MySQLValueProjectionParticipant(session, desired).prepare(table)
    await prepared.apply(
        MaterializedRowsChanged(
            table=table,
            before_rows=({"id": 1},),
            after_rows=({"id": 2},),
            checkpoint={"coordinate": {"position": 12}},
        )
    )

    assert [name for name, _ in observed] == ["prepare", "delta", "desired"]
    assert all(actual_session is session for _, actual_session in observed)
