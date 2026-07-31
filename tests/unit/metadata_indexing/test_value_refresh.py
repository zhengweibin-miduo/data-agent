"""字段值精确频次与稳定发布身份契约。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
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
from data_agent.metadata_indexing.models import (
    MetadataIndexOperation,
    MetadataIndexTarget,
    MetadataObjectKind,
    MetadataValueRefreshPhase,
)
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

    async def rows_are_counted(
        session: AsyncSession,
        state: FrequencyMutationState,
        column_id: str,
        rows: list[dict[str, object]],
    ) -> list[bool]:
        del session, state, column_id
        return [int(str(row["id"])) <= 10 for row in rows]

    monkeypatch.setattr(value_refresh, "_rows_are_counted", rows_are_counted)
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


async def test_scan_cursor_batches_row_boundary_comparisons() -> None:
    """同一批 CDC 行的游标边界必须由一次集合化查询完成。"""
    statements: list[ClauseElement] = []

    class FakeResult:
        def one(self) -> tuple[bool, ...]:
            return (True, False, True)

    class FakeSession:
        async def execute(self, statement: ClauseElement) -> FakeResult:
            statements.append(statement)
            return FakeResult()

    state = _state(
        MetadataValueRefreshPhase.SCAN,
        progress="region-id",
        cursor=(10,),
    )
    counted = await value_refresh._rows_are_counted(
        cast(AsyncSession, FakeSession()),
        state,
        "region-id",
        [
            {"id": 8, "region": "a"},
            {"id": 12, "region": "b"},
            {"id": 9, "region": "c"},
        ],
    )

    check_equal("批量边界结果", counted, [True, False, True])
    check_equal("边界查询次数", len(statements), 1)


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


def test_mysql_cursor_comparison_uses_dw_binary_collation_for_text_keys() -> None:
    """字符串边界比较必须与 DW keyset scan 使用相同的二进制排序规则。"""
    expression = value_refresh._mysql_order_value("a", "VARCHAR(64)")
    rendered = str(expression.compile(compile_kwargs={"literal_binds": True})).lower()

    check_condition(
        "字符串主键使用 DW 排序规则",
        "collate utf8mb4_0900_bin" in rendered,
        actual=rendered,
        expected="literal 显式使用 utf8mb4_0900_bin",
    )


def test_data_sync_skips_old_scan_cursor_for_pending_structure_generation() -> None:
    """结构代次 pending 后 data-sync 不得用新 plan 解码旧 SCAN 游标。"""
    check_equal(
        "结构代次变化需要跳过旧增量",
        value_refresh._has_pending_structure_generation(
            {
                "pending_desired_version": "desired-v2",
                "pending_frequency_version": "frequency-v2",
                "frequency_version": "frequency-v1",
            }
        ),
        True,
    )
    check_equal(
        "同频 pending 仍可维护当前基线",
        value_refresh._has_pending_structure_generation(
            {
                "pending_desired_version": "desired-v2",
                "pending_frequency_version": "frequency-v1",
                "frequency_version": "frequency-v1",
            }
        ),
        False,
    )


async def test_select_top_n_promotes_pending_before_reading_current_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SELECT_TOP_N 必须先提升结构代次再解释旧字段进度。"""
    calls: list[str] = []

    class FakeOutbox:
        def __init__(self, session: AsyncSession) -> None:
            del session

        async def lock_authoritative(self, item: object) -> bool:
            del item
            calls.append("lock")
            return True

        async def promote_pending_value_state(self, item: object) -> bool:
            del item
            calls.append("promote")
            return True

    @asynccontextmanager
    async def session_context() -> AsyncIterator[AsyncSession]:
        yield cast(AsyncSession, object())

    refresh = value_refresh.MetadataValueRefresh()

    async def forbidden_plan(session: AsyncSession, table_id: str) -> None:
        del session, table_id
        pytest.fail("pending 提升前不得读取当前结构 plan")

    monkeypatch.setattr(value_refresh, "MetadataIndexOutboxRepository", FakeOutbox)
    monkeypatch.setattr(value_refresh.MySQLDatabase, "session", session_context)
    monkeypatch.setattr(refresh, "_plan", forbidden_plan)

    item = value_refresh.ClaimedMetadataIndexWork(
        target=MetadataIndexTarget.VALUES,
        object_kind=MetadataObjectKind.TABLE,
        object_id="table-a",
        operation=MetadataIndexOperation.REFRESH,
        desired_version="v1",
        frequency_version="f1",
        lease_token="a" * 32,
        progress_column_id="removed-column",
        phase=MetadataValueRefreshPhase.SELECT_TOP_N,
        index_generation="g1",
    )
    await refresh._select_top_n(item)

    check_equal("SELECT_TOP_N 抢占顺序", calls, ["lock", "promote"])


def test_document_id_is_scoped_by_table_column_and_value_hash() -> None:
    """相同规范值在不同 table/column 下不能共享 Elasticsearch ID。"""
    value_hash = "a" * 64
    identifiers = {
        metadata_value_document_id("table-a", "column-a", value_hash),
        metadata_value_document_id("table-b", "column-a", value_hash),
        metadata_value_document_id("table-a", "column-b", value_hash),
    }
    check_equal("稳定 ID 同时包含三层身份", len(identifiers), 3)


async def test_publication_candidate_ids_budget_before_payload_read() -> None:
    """发布候选必须先用轻量长度查询裁剪，再读取 LONGTEXT/JSON 正文。"""
    class FakeMappings:
        def __init__(self, rows: list[dict[str, object]]) -> None:
            self.rows = rows

        def mappings(self) -> "FakeMappings":
            return self

        def all(self) -> list[dict[str, object]]:
            return self.rows

    class FakeSession:
        def __init__(self) -> None:
            self.statements: list[ClauseElement] = []

        async def execute(self, statement: ClauseElement) -> FakeMappings:
            self.statements.append(statement)
            return FakeMappings(
                [
                    {"document_id": "doc-1", "estimated_bytes": 3_000_000},
                    {"document_id": "doc-2", "estimated_bytes": 3_000_000},
                ]
            )

    session = FakeSession()
    repository = value_refresh.MetadataValueFrequencyRepository(
        cast(AsyncSession, session)
    )
    item = value_refresh.ClaimedMetadataIndexWork(
        target=MetadataIndexTarget.VALUES,
        object_kind=MetadataObjectKind.TABLE,
        object_id="table-a",
        operation=MetadataIndexOperation.REFRESH,
        desired_version="desired-v1",
        frequency_version="frequency-v1",
        lease_token="a" * 32,
        phase=MetadataValueRefreshPhase.PUBLISH,
        index_generation="generation-v1",
    )

    document_ids = await repository._publication_candidate_ids(item, "upsert")

    check_equal("字节预算内候选", document_ids, ["doc-1"])
    rendered = str(session.statements[0])
    check_condition(
        "轻量查询不读取 LONGTEXT 正文",
        "value_text," not in rendered and "octet_length" in rendered.lower(),
        actual=rendered,
        expected="仅选择 document_id 与估算字节数",
    )


async def test_publication_candidate_ids_seek_after_persisted_cursor() -> None:
    """已结算发布批次后的候选查询必须从持久化文档游标继续。"""
    class FakeMappings:
        def __init__(self, rows: list[dict[str, object]]) -> None:
            self.rows = rows

        def mappings(self) -> "FakeMappings":
            return self

        def all(self) -> list[dict[str, object]]:
            return self.rows

    class FakeSession:
        def __init__(self) -> None:
            self.statements: list[ClauseElement] = []

        async def execute(self, statement: ClauseElement) -> FakeMappings:
            self.statements.append(statement)
            return FakeMappings([])

    session = FakeSession()
    repository = value_refresh.MetadataValueFrequencyRepository(
        cast(AsyncSession, session)
    )
    item = value_refresh.ClaimedMetadataIndexWork(
        target=MetadataIndexTarget.VALUES,
        object_kind=MetadataObjectKind.TABLE,
        object_id="table-a",
        operation=MetadataIndexOperation.REFRESH,
        desired_version="desired-v1",
        frequency_version="frequency-v1",
        lease_token="a" * 32,
        phase=MetadataValueRefreshPhase.PUBLISH,
        index_generation="generation-v1",
        bulk_cursor={
            "phase": "publish",
            "desired_version": "desired-v1",
            "index_generation": "generation-v1",
            "last_document_id": "doc-500",
        },
    )

    await repository._publication_candidate_ids(item, "upsert")

    check_equal("先查询待重放动作再执行 keyset", len(session.statements), 2)
    rendered = str(
        session.statements[1].compile(compile_kwargs={"literal_binds": True})
    )
    check_condition("发布查询使用持久化游标", "doc-500" in rendered and ">" in rendered)


async def test_cleanup_deletes_stale_never_published_rows_locally() -> None:
    """过期且从未发布的 membership 必须有界本地回收。"""
    class FakeScalars:
        def all(self) -> list[str]:
            return ["doc-old"]

    class FakeResult:
        def scalars(self) -> FakeScalars:
            return FakeScalars()

    class FakeSession:
        def __init__(self) -> None:
            self.statements: list[ClauseElement] = []

        async def execute(self, statement: ClauseElement) -> FakeResult:
            self.statements.append(statement)
            return FakeResult()

    session = FakeSession()
    repository = value_refresh.MetadataValueFrequencyRepository(
        cast(AsyncSession, session)
    )
    item = value_refresh.ClaimedMetadataIndexWork(
        target=MetadataIndexTarget.VALUES,
        object_kind=MetadataObjectKind.TABLE,
        object_id="table-a",
        operation=MetadataIndexOperation.REFRESH,
        desired_version="desired-v2",
        frequency_version="frequency-v2",
        lease_token="a" * 32,
        phase=MetadataValueRefreshPhase.CLEANUP,
        index_generation="generation-v1",
    )

    deleted = await repository.delete_stale_unpublished_batch(item)

    check_equal("回收行数", deleted, 1)
    check_equal("先锁定再删除", len(session.statements), 2)


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
