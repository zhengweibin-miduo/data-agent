"""字段值精确频次与稳定发布身份契约。"""

from datetime import date, datetime
from decimal import Decimal
from typing import cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ClauseElement
from tests.helpers.checks import check_condition, check_equal

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

        async def apply_deltas(self, **kwargs: object) -> None:
            calls.extend(
                (str(value), int(amount))
                for value, amount in cast(dict[str, int], kwargs["deltas"]).items()
            )

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

        async def apply_deltas(self, **kwargs: object) -> None:
            calls.extend(
                (str(value), int(amount))
                for value, amount in cast(dict[str, int], kwargs["deltas"]).items()
            )

    monkeypatch.setattr(
        value_refresh,
        "MetadataValueFrequencyRepository",
        FakeRepository,
    )

    async def row_is_counted(
        session: AsyncSession,
        state: FrequencyMutationState,
        column_id: str,
        row: dict[str, object],
    ) -> bool:
        del session, state, column_id
        return int(str(row["id"])) <= 10

    monkeypatch.setattr(value_refresh, "_row_is_counted", row_is_counted)
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


async def test_scan_cursor_uses_mysql_enum_and_set_order() -> None:
    """CDC 游标边界必须由 MySQL ENUM/SET 声明顺序表达式判定。"""
    statements: list[ClauseElement] = []

    class FakeSession:
        async def scalar(self, statement: ClauseElement) -> bool:
            statements.append(statement)
            return True

    state = _state(
        MetadataValueRefreshPhase.SCAN,
        progress="region-id",
        cursor=("a", "x,y"),
    )
    desired = state.plan.desired.model_copy(
        update={
            "columns": [
                state.plan.desired.columns[0].model_copy(
                    update={"data_type": "ENUM('z','a')"}
                ),
                state.plan.desired.columns[1],
                DesiredColumn(
                    id="flags-id",
                    name="flags",
                    data_type="SET('x','y')",
                    nullable=False,
                ),
            ],
            "primary_key": ["id", "flags"],
        }
    )
    enum_set_state = FrequencyMutationState(
        table_id=state.table_id,
        phase=state.phase,
        frequency_version=state.frequency_version,
        progress_column_id=state.progress_column_id,
        last_primary_key=state.last_primary_key,
        plan=ValueProjectionPlan(desired=desired, columns=state.plan.columns),
    )

    counted = await value_refresh._row_is_counted(
        cast(AsyncSession, FakeSession()),
        enum_set_state,
        "region-id",
        {"id": "z", "flags": "y", "region": "华东"},
    )

    check_equal("数据库边界判断结果", counted, True)
    rendered = str(
        statements[0].compile(compile_kwargs={"literal_binds": True})
    ).lower()
    check_condition("ENUM 使用 MySQL FIELD 顺序", "field(" in rendered)
    check_condition("SET 使用 MySQL FIND_IN_SET 位序", "find_in_set(" in rendered)


@pytest.mark.parametrize(
    ("value", "data_type"),
    [
        (Decimal("12.50"), "DECIMAL(10,2)"),
        (date(2026, 7, 30), "DATE"),
        (datetime(2026, 7, 30, 12, 30), "DATETIME"),
        (b"\x01\x02", "BINARY(2)"),
    ],
)
def test_mysql_cursor_comparison_binds_native_primary_key_values(
    value: object,
    data_type: str,
) -> None:
    """非 ENUM/SET 主键必须以驱动可识别的原生类型参与 MySQL 排序。"""
    expression = value_refresh._mysql_order_value(value, data_type)
    parameters = expression.compile().params
    check_equal("原生主键绑定值", list(parameters.values()), [value])


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
