"""字段值精确频次与稳定发布身份契约。"""

from collections.abc import AsyncIterator, Iterator
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


class _MappingResult:
    """为 SQLAlchemy mappings 查询提供有序假结果。"""

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> "_MappingResult":
        """返回 mappings 结果自身。"""
        return self

    def all(self) -> list[dict[str, object]]:
        """返回全部映射行。"""
        return self._rows

    def one_or_none(self) -> dict[str, object] | None:
        """返回唯一映射行或空值。"""
        return self._rows[0] if self._rows else None


class _ScalarResult:
    """为 SQLAlchemy scalars 查询提供可迭代假结果。"""

    def __init__(self, values: list[object]) -> None:
        self._values = values

    def __iter__(self) -> Iterator[object]:
        """按 SQLAlchemy ScalarResult 语义返回迭代器。"""
        return iter(self._values)

    def all(self) -> list[object]:
        """返回全部标量。"""
        return self._values


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


async def test_incremental_changes_skip_values_over_scan_byte_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CDC、回填与 reset 必须像 SCAN 一样排除确定性超限值。"""
    calls: list[dict[str, int]] = []

    class FakeRepository:
        """记录增量频次变化。"""

        def __init__(self, session: AsyncSession) -> None:
            del session

        async def apply_deltas(self, **kwargs: object) -> None:
            calls.append(cast(dict[str, int], kwargs["deltas"]))

    monkeypatch.setattr(
        value_refresh,
        "MetadataValueFrequencyRepository",
        FakeRepository,
    )
    oversized = "长" * value_refresh._VALUE_READ_BYTE_LIMIT
    await value_refresh.apply_frequency_row_changes(
        cast(AsyncSession, object()),
        [_state(MetadataValueRefreshPhase.COMPLETE)],
        [
            {"id": 1, "region": oversized},
            {"id": 2, "region": "旧值"},
            {"id": 3, "region": oversized},
        ],
        [
            {"id": 1, "region": "新增"},
            {"id": 2, "region": oversized},
        ],
    )

    check_equal(
        "增量频次统一排除超限 before/after 值",
        calls,
        [{"旧值": -1, "新增": 1}],
    )


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


async def test_prepare_frequency_mutation_keeps_only_current_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共享目标的 CDC 只能维护当前物理来源对应的逻辑字段。"""
    source_a = _state(MetadataValueRefreshPhase.COMPLETE).plan.desired
    source_b = source_a.model_copy(
        update={
            "source": "source-b",
            "source_table": "orders_peer",
            "columns": [
                source_a.columns[0].model_copy(update={"id": "peer-id"}),
                source_a.columns[1].model_copy(update={"id": "peer-region-id"}),
            ],
        }
    )
    outbox_row = {
        "frequency_version": "f" * 64,
        "phase": MetadataValueRefreshPhase.COMPLETE.value,
        "progress_column_id": None,
        "last_primary_key": None,
        "pending_desired_version": None,
        "pending_frequency_version": None,
    }

    class FakeSession:
        """按调用顺序返回 peer、Meta 表及 outbox 状态。"""

        def __init__(self) -> None:
            self.scalar_calls = 0

        async def scalars(self, statement: ClauseElement) -> _ScalarResult:
            del statement
            self.scalar_calls += 1
            if self.scalar_calls == 1:
                return _ScalarResult(
                    [
                        source_a.model_dump(mode="json"),
                        source_b.model_dump(mode="json"),
                    ]
                )
            return _ScalarResult(["table-a", "table-b"])

        async def execute(self, statement: ClauseElement) -> _MappingResult:
            del statement
            return _MappingResult([outbox_row])

    class FakeProjectionRepository:
        """按逻辑表返回各自来源的值投影计划。"""

        def __init__(self, session: AsyncSession) -> None:
            del session

        async def value_projection_plan(self, table_id: str) -> ValueProjectionPlan:
            desired = source_a if table_id == "table-a" else source_b
            column = desired.columns[1]
            return ValueProjectionPlan(
                desired=desired,
                columns=((column.id, column.name, column.data_type),),
            )

    monkeypatch.setattr(
        value_refresh, "MetadataProjectionRepository", FakeProjectionRepository
    )
    states = await value_refresh.prepare_frequency_mutation(
        cast(AsyncSession, FakeSession()), source_a
    )

    check_equal("当前来源状态数量", len(states), 1)
    check_equal("当前来源逻辑表", states[0].table_id, "table-a")


async def test_scan_preflights_length_and_filters_key_owner() -> None:
    """SCAN 必须先读长度，再按主键 owner 只读取当前来源的正文。"""
    plan = _state(MetadataValueRefreshPhase.SCAN).plan
    owned_row = {"id": 1}
    oversized_row = {"id": 2}
    owned_hash = value_refresh.primary_key_identity(plan.desired, owned_row)[1]
    oversized_hash = value_refresh.primary_key_identity(plan.desired, oversized_row)[1]

    class FakeSession:
        """模拟轻量预检、owner 查询与正文回读。"""

        def __init__(self) -> None:
            self.statements: list[ClauseElement] = []

        async def execute(self, statement: ClauseElement) -> _MappingResult:
            self.statements.append(statement)
            if len(self.statements) == 1:
                return _MappingResult(
                    [
                        {"id": 1, "value_bytes": 16},
                        {"id": 2, "value_bytes": 5_000_000},
                        {"id": 3, "value_bytes": 16},
                    ]
                )
            return _MappingResult([{"id": 1, "region": "华东"}])

        async def scalars(self, statement: ClauseElement) -> _ScalarResult:
            self.statements.append(statement)
            return _ScalarResult([owned_hash, oversized_hash])

    session = FakeSession()
    result = await value_refresh.MetadataValueFrequencyRepository(
        cast(AsyncSession, session)
    ).scan_rows(plan, plan.columns[0], None)

    check_equal("仅回读当前来源预算内正文", result.rows, ({"id": 1, "region": "华东"},))
    check_equal("游标越过超限值与外源行", result.last_primary_key, (3,))
    preflight_sql = str(session.statements[0]).lower()
    check_condition(
        "预检只通过 OCTET_LENGTH 读取正文长度",
        "octet_length" in preflight_sql and "region AS" not in preflight_sql,
        actual=preflight_sql,
        expected="主键加 OCTET_LENGTH(value)",
    )


async def test_scan_applies_normalized_value_byte_limit() -> None:
    """Base64 膨胀后超限的二进制值不得进入精确频次。"""
    captured: list[dict[str, int]] = []

    class RecordingRepository(value_refresh.MetadataValueFrequencyRepository):
        """仅记录 SCAN 生成的增量。"""

        async def apply_deltas(self, **kwargs: object) -> None:
            captured.append(cast(dict[str, int], kwargs["deltas"]))

    raw_value = b"x" * 3_200_000
    repository = RecordingRepository(cast(AsyncSession, object()))
    await repository.add_scan_values(
        table_id="table-a",
        frequency_version="frequency-v1",
        column_item=("blob-id", "payload", "LONGBLOB"),
        rows=({"payload": raw_value},),
    )

    check_equal("SCAN 排除规范化后超限值", captured, [{}])


async def test_scan_stops_before_value_that_exceeds_remaining_batch_budget() -> None:
    """SCAN 总预算不足时必须停在前一主键并留待下一 claim。"""
    plan = _state(MetadataValueRefreshPhase.SCAN).plan
    hashes = [
        value_refresh.primary_key_identity(plan.desired, {"id": value})[1]
        for value in (1, 2)
    ]

    class FakeSession:
        """返回两个单独可索引但合计超出批次预算的值。"""

        def __init__(self) -> None:
            self.execute_calls = 0

        async def execute(self, statement: ClauseElement) -> _MappingResult:
            del statement
            self.execute_calls += 1
            if self.execute_calls == 1:
                return _MappingResult(
                    [
                        {"id": 1, "value_bytes": 2_500_000},
                        {"id": 2, "value_bytes": 2_000_000},
                    ]
                )
            return _MappingResult([{"id": 1, "region": "a"}])

        async def scalars(self, statement: ClauseElement) -> _ScalarResult:
            del statement
            return _ScalarResult(list(hashes))

    result = await value_refresh.MetadataValueFrequencyRepository(
        cast(AsyncSession, FakeSession())
    ).scan_rows(plan, plan.columns[0], None)

    check_equal("批次预算前缀正文", result.rows, ({"id": 1, "region": "a"},))
    check_equal("预算边界游标", result.last_primary_key, (1,))


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


async def test_select_top_n_persists_byte_bounded_rank_cursor() -> None:
    """Top-N 必须按稳定排名持久化一个字节预算内的 V1 前缀。"""
    plan = _state(MetadataValueRefreshPhase.COMPLETE).plan
    item = value_refresh.ClaimedMetadataIndexWork(
        target=MetadataIndexTarget.VALUES,
        object_kind=MetadataObjectKind.TABLE,
        object_id="table-a",
        operation=MetadataIndexOperation.REFRESH,
        desired_version="desired-v1",
        frequency_version="frequency-v1",
        lease_token="a" * 32,
        phase=MetadataValueRefreshPhase.SELECT_TOP_N,
        index_generation="generation-v1",
    )

    class FakeSession:
        """返回合计超过单 claim 预算的稳定排名行。"""

        def __init__(self) -> None:
            self.statements: list[ClauseElement] = []

        async def execute(self, statement: ClauseElement) -> _MappingResult:
            self.statements.append(statement)
            if len(self.statements) == 1:
                return _MappingResult(
                    [
                        {
                            "value_hash": "a" * 64,
                            "frequency": 20,
                            "estimated_bytes": 3_000_000,
                        },
                        {
                            "value_hash": "b" * 64,
                            "frequency": 10,
                            "estimated_bytes": 3_000_000,
                        },
                    ]
                )
            if len(self.statements) == 2:
                return _MappingResult(
                    [
                        {
                            "value_hash": "a" * 64,
                            "value_text": "华东",
                            "frequency": 20,
                        }
                    ]
                )
            return _MappingResult([])

    session = FakeSession()
    result = await value_refresh.MetadataValueFrequencyRepository(
        cast(AsyncSession, session)
    ).materialize_top_n(item, plan, plan.columns[0])

    check_equal("Top-N 首批尚未完成", result.completed, False)
    check_equal("Top-N V1 游标版本", result.cursor and result.cursor["v"], 1)
    check_equal(
        "Top-N 稳定排名游标",
        result.cursor and result.cursor["last_value_hash"],
        "a" * 64,
    )
    check_equal(
        "Top-N 已检查排名数", result.cursor and result.cursor["ranked_count"], 1
    )
    preflight_sql = str(session.statements[0]).lower()
    check_condition(
        "Top-N 预检不直接选择 LONGTEXT",
        "octet_length" in preflight_sql and "value_text," not in preflight_sql,
        actual=preflight_sql,
        expected="hash、frequency 与 OCTET_LENGTH(value_text)",
    )


async def test_select_top_n_skips_single_oversized_value_without_payload_read() -> None:
    """单个超限 Top-N 值必须推进排名且不读取正文或无限重试。"""
    plan = _state(MetadataValueRefreshPhase.COMPLETE).plan
    item = value_refresh.ClaimedMetadataIndexWork(
        target=MetadataIndexTarget.VALUES,
        object_kind=MetadataObjectKind.TABLE,
        object_id="table-a",
        operation=MetadataIndexOperation.REFRESH,
        desired_version="desired-v1",
        frequency_version="frequency-v1",
        lease_token="a" * 32,
        phase=MetadataValueRefreshPhase.SELECT_TOP_N,
        index_generation="generation-v1",
    )

    class FakeSession:
        """仅返回一个确定性超限的轻量排名行。"""

        def __init__(self) -> None:
            self.execute_calls = 0

        async def execute(self, statement: ClauseElement) -> _MappingResult:
            del statement
            self.execute_calls += 1
            return _MappingResult(
                [
                    {
                        "value_hash": "z" * 64,
                        "frequency": 1,
                        "estimated_bytes": value_refresh._VALUE_READ_BYTE_LIMIT + 1,
                    }
                ]
            )

    session = FakeSession()
    result = await value_refresh.MetadataValueFrequencyRepository(
        cast(AsyncSession, session)
    ).materialize_top_n(item, plan, plan.columns[0])

    check_equal("超限值后字段收敛", result.completed, True)
    check_equal("超限值未读取正文", session.execute_calls, 1)


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
