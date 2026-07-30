"""字段值精确频次与稳定发布身份契约。"""

from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests.helpers.checks import check_equal

from data_agent.data_sync.models import DesiredColumn, DesiredSyncTable
from data_agent.metadata_indexing import value_refresh
from data_agent.metadata_indexing.elasticsearch import metadata_value_document_id
from data_agent.metadata_indexing.models import MetadataValueRefreshPhase
from data_agent.metadata_indexing.projections import ValueProjectionPlan
from data_agent.metadata_indexing.value_refresh import FrequencyMutationState


def _state(
    phase: MetadataValueRefreshPhase,
    *,
    progress: str | None = None,
    cursor: tuple[object, ...] | None = None,
) -> FrequencyMutationState:
    desired = DesiredSyncTable(
        source="source-a",
        source_schema="business",
        source_table="orders",
        target_table="orders",
        columns=[
            DesiredColumn(id="id", name="id", data_type="BIGINT", nullable=False),
            DesiredColumn(
                id="region-id",
                name="region",
                data_type="VARCHAR(64)",
                nullable=False,
            ),
        ],
        primary_key=["id"],
        schema_fingerprint="s" * 64,
    )
    return FrequencyMutationState(
        table_id="table-a",
        phase=phase,
        frequency_version="f" * 64,
        progress_column_id=progress,
        last_primary_key=cursor,
        plan=ValueProjectionPlan(
            desired=desired,
            columns=(("region-id", "region", "VARCHAR(64)"),),
        ),
    )


async def test_cdc_update_applies_old_minus_one_and_new_plus_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UPDATE 必须在同一调用中产生精确旧值减、新值加。"""
    calls: list[tuple[str, int]] = []

    class FakeRepository:
        """记录精确 delta。"""

        def __init__(self, session: AsyncSession) -> None:
            del session

        async def apply_delta(self, **kwargs: object) -> None:
            calls.append((str(kwargs["value_text"]), int(str(kwargs["delta"]))))

    monkeypatch.setattr(
        value_refresh,
        "MetadataValueFrequencyRepository",
        FakeRepository,
    )
    await value_refresh.apply_frequency_row_changes(
        cast(AsyncSession, object()),
        [_state(MetadataValueRefreshPhase.COMPLETE)],
        [{"id": 1, "region": "华东"}],
        [{"id": 1, "region": "华西"}],
    )
    check_equal("CDC UPDATE 精确 delta", calls, [("华东", -1), ("华西", 1)])


async def test_scan_cursor_only_applies_cdc_to_already_scanned_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SCAN 期间游标前事件走 delta，游标后事件留给后续原始行扫描。"""
    calls: list[tuple[str, int]] = []

    class FakeRepository:
        """记录游标边界内 delta。"""

        def __init__(self, session: AsyncSession) -> None:
            del session

        async def apply_delta(self, **kwargs: object) -> None:
            calls.append((str(kwargs["value_text"]), int(str(kwargs["delta"]))))

    monkeypatch.setattr(
        value_refresh,
        "MetadataValueFrequencyRepository",
        FakeRepository,
    )
    state = _state(
        MetadataValueRefreshPhase.SCAN,
        progress="region-id",
        cursor=(10,),
    )
    await value_refresh.apply_frequency_row_changes(
        cast(AsyncSession, object()),
        [state],
        [],
        [
            {"id": 8, "region": "已扫描"},
            {"id": 12, "region": "待扫描"},
        ],
    )
    check_equal("SCAN 条件频次维护", calls, [("已扫描", 1)])


def test_document_id_is_scoped_by_table_column_and_value_hash() -> None:
    """相同规范值在不同 table/column 下不能共享 Elasticsearch ID。"""
    value_hash = "a" * 64
    identifiers = {
        metadata_value_document_id("table-a", "column-a", value_hash),
        metadata_value_document_id("table-b", "column-a", value_hash),
        metadata_value_document_id("table-a", "column-b", value_hash),
    }
    check_equal("稳定 ID 同时包含三层身份", len(identifiers), 3)


def test_scan_cursor_rejects_schema_or_primary_key_identity_mismatch() -> None:
    """恢复 SCAN 时不能把旧 schema 的同长度游标误当成当前游标。"""
    plan = _state(MetadataValueRefreshPhase.SCAN).plan
    cursor = value_refresh._cursor_values(plan, [10])

    check_equal("游标版本", cursor["v"], 1)
    check_equal("游标 schema", cursor["schema_fingerprint"], "s" * 64)
    check_equal("游标主键列", cursor["columns"], ["id"])
    check_equal("游标主键类型", cursor["types"], ["BIGINT"])
    check_equal("游标恢复", value_refresh._decoded_cursor(plan, cursor), (10,))

    stale_cursor = {**cursor, "schema_fingerprint": "old"}
    with pytest.raises(ValueError, match="schema 或主键定义不匹配"):
        value_refresh._decoded_cursor(plan, stale_cursor)
