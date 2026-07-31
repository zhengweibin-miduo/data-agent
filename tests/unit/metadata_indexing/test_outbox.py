"""元数据索引 desired state 与租约结算契约。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import patch

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ClauseElement
from tests.helpers.checks import check_condition, check_equal
from tests.helpers.factories import semantic_for

from data_agent.data_sync.models import DesiredColumn, DesiredSyncTable
from data_agent.ddl_metadata.parsing import parse_ddl
from data_agent.metadata_indexing.desired import (
    enqueue_value_refresh,
    semantic_desired_states,
    shared_value_refresh_states,
)
from data_agent.metadata_indexing.models import (
    ClaimedMetadataIndexWork,
    MetadataIndexDesired,
    MetadataIndexOperation,
    MetadataIndexTarget,
    MetadataObjectKind,
    MetadataValueRefreshPhase,
)
from data_agent.metadata_indexing.repository import MetadataIndexOutboxRepository
from data_agent.models.semantic import MetricMetadata


class _FakeResult:
    """提供预置行、标量及受影响行数。"""

    def __init__(self, values: list[Any] | None = None, rowcount: int = 1) -> None:
        """绑定结果。"""
        self._values = values or []
        self.rowcount = rowcount

    def mappings(self) -> _FakeResult:
        """返回映射视图。"""
        return self

    def all(self) -> list[Any]:
        """返回全部结果。"""
        return self._values

    def one_or_none(self) -> Any | None:
        """返回唯一映射或空。"""
        return self._values[0] if self._values else None


class _RecordingSession:
    """记录 SQL 并按顺序返回预置结果。"""

    def __init__(self, results: list[_FakeResult] | None = None) -> None:
        """绑定预置执行结果。"""
        self.statements: list[ClauseElement] = []
        self._results = list(results or [])

    async def execute(self, statement: ClauseElement) -> _FakeResult:
        """记录语句。"""
        self.statements.append(statement)
        return self._results.pop(0) if self._results else _FakeResult()

    async def scalar(self, statement: ClauseElement) -> int:
        """记录标量查询并返回存在计数。"""
        self.statements.append(statement)
        return 1


async def test_semantic_enqueue_assigns_inserted_version_without_empty_case() -> None:
    """纯 semantic UPSERT 的版本赋值必须生成合法 MySQL SQL。"""
    session = _RecordingSession()
    await MetadataIndexOutboxRepository(cast(AsyncSession, session)).enqueue(
        [
            MetadataIndexDesired(
                target=MetadataIndexTarget.SEMANTIC,
                object_kind=MetadataObjectKind.TABLE,
                object_id="table-1",
                operation=MetadataIndexOperation.UPSERT,
                desired_version="v" * 64,
            )
        ]
    )

    sql = str(session.statements[0].compile(dialect=mysql.dialect()))
    check_condition(
        "semantic UPSERT 不生成空 CASE",
        "CASE ELSE" not in sql,
        actual=sql,
        expected="desired_version 直接赋值为 inserted alias",
    )


async def test_shared_eligibility_change_refreshes_every_peer_table() -> None:
    """共享目标任一字段资格变化必须刷新所有关联 Meta 表。"""
    profile = {
        "decision": "skip",
        "sensitivity": "sensitive",
        "reason": "测试敏感字段",
        "evidence": ["column-b"],
    }
    peers = [
        DesiredSyncTable(
            source=source,
            source_schema="sales",
            source_table=f"orders_{source}",
            target_table="orders",
            columns=[
                DesiredColumn(
                    id=f"column-{source}",
                    name="region",
                    data_type="VARCHAR(64)",
                    nullable=False,
                )
            ],
            primary_key=["region"],
            schema_fingerprint=source * 64,
        )
        for source in ("a", "b")
    ]

    class FakeScalars:
        """返回共享 DW 任务载荷。"""

        def all(self) -> list[dict[str, object]]:
            """返回两个来源的期望状态。"""
            return [peer.model_dump(mode="json") for peer in peers]

    class FakeRows:
        """返回字段到 Meta 表及资格的映射。"""

        def mappings(self) -> FakeRows:
            """保持映射结果接口。"""
            return self

        def __iter__(self) -> Iterator[dict[str, object]]:
            """遍历字段映射。"""
            return iter(
                [
                    {"id": "column-a", "table_id": "table-a", "index_profile": profile},
                    {"id": "column-b", "table_id": "table-b", "index_profile": profile},
                ]
            )

    class FakeSession:
        """提供共享刷新查询所需的最小 Session 接口。"""

        async def scalars(self, statement: object) -> FakeScalars:
            """忽略 SQL 并返回 peer 任务。"""
            del statement
            return FakeScalars()

        async def execute(self, statement: object) -> FakeRows:
            """忽略 SQL 并返回字段权威信息。"""
            del statement
            return FakeRows()

    states = await shared_value_refresh_states(
        cast(AsyncSession, FakeSession()), {"orders"}
    )

    check_equal(
        "共享资格变化刷新全部表",
        {state.object_id for state in states},
        {"table-a", "table-b"},
    )

    changed_peers = [
        peers[0],
        peers[1].model_copy(update={"schema_fingerprint": "c" * 64}),
    ]

    class ChangedScalars(FakeScalars):
        """返回仅全局投影指纹变化的共享任务载荷。"""

        def all(self) -> list[dict[str, object]]:
            """保持物理 desired hash，仅替换投影使用的指纹。"""
            return [peer.model_dump(mode="json") for peer in changed_peers]

    class ChangedSession(FakeSession):
        """提供变更后的共享任务。"""

        async def scalars(self, statement: object) -> ChangedScalars:
            """忽略 SQL 并返回变更后的 peer 任务。"""
            del statement
            return ChangedScalars()

    changed_states = await shared_value_refresh_states(
        cast(AsyncSession, ChangedSession()), {"orders"}
    )
    check_condition(
        "共享投影指纹变化生成新值版本",
        states[0].desired_version != changed_states[0].desired_version,
        expected="schema_fingerprint 变化必须替换 VALUES desired_version",
    )


@pytest.mark.asyncio
async def test_frequency_version_is_independent_of_triggering_peer() -> None:
    """共享目标的频次代次必须由全部 peer 决定，而非当前事件来源。"""
    peers = [
        DesiredSyncTable(
            source=source,
            source_schema="sales",
            source_table=f"orders_{source}",
            target_table="orders",
            columns=[
                DesiredColumn(
                    id=f"column-{source}",
                    name=f"region_{source}",
                    data_type="VARCHAR(64)",
                    nullable=False,
                )
            ],
            primary_key=[f"region_{source}"],
            schema_fingerprint=source * 64,
        )
        for source in ("a", "b")
    ]

    class FakeScalars:
        def all(self) -> list[dict[str, object]]:
            return [peer.model_dump(mode="json") for peer in peers]

    class FakeTableScalars:
        def __iter__(self) -> Iterator[str]:
            return iter(["table-a", "table-b"])

    class FakeRows:
        def mappings(self) -> FakeRows:
            return self

        def __iter__(self) -> Iterator[dict[str, object]]:
            return iter(
                {
                    "id": f"column-{source}",
                    "table_id": f"table-{source}",
                    "index_profile": {"decision": "index"},
                }
                for source in ("a", "b")
            )

    class FakeSession:
        async def scalars(self, statement: object) -> FakeScalars | FakeTableScalars:
            rendered = str(statement)
            if "desired_json" in rendered:
                return FakeScalars()
            return FakeTableScalars()

        async def execute(self, statement: object) -> FakeRows:
            del statement
            return FakeRows()

    captured: list[list[MetadataIndexDesired]] = []

    async def capture_enqueue(
        self: MetadataIndexOutboxRepository,
        states: list[MetadataIndexDesired],
        *,
        debounce_seconds: int | None = None,
    ) -> None:
        del self, debounce_seconds
        captured.append(states)

    with patch.object(MetadataIndexOutboxRepository, "enqueue", capture_enqueue):
        for peer in peers:
            await enqueue_value_refresh(
                cast(AsyncSession, FakeSession()), peer, {"position": 1}
            )

    check_equal(
        "不同来源事件生成相同频次代次",
        {state.frequency_version for states in captured for state in states},
        {captured[0][0].frequency_version},
    )


def _rendered(statement: ClauseElement) -> str:
    """使用 MySQL 方言渲染语句和参数。"""
    compiled = statement.compile(dialect=mysql.dialect())
    return f"{compiled} {compiled.params}"


def _work(token: str = "a" * 32) -> ClaimedMetadataIndexWork:
    """构造一条已领取工作。"""
    return ClaimedMetadataIndexWork(
        target=MetadataIndexTarget.SEMANTIC,
        object_kind=MetadataObjectKind.COLUMN,
        object_id="column-1",
        operation=MetadataIndexOperation.UPSERT,
        desired_version="v" * 64,
        lease_token=token,
    )


async def test_claim_uses_database_clock_and_excludes_dead_letters() -> None:
    """领取必须短锁、数据库时钟租约并过滤死信。"""
    row = _work().model_dump(mode="json", exclude={"lease_token"})
    row["progress_column_id"] = "column-0"
    session = _RecordingSession([_FakeResult([row]), _FakeResult()])
    repository = MetadataIndexOutboxRepository(cast(AsyncSession, session))

    claimed = await repository.claim(1)

    check_equal("领取一条工作", len(claimed), 1)
    check_equal("领取携带持久化字段游标", claimed[0].progress_column_id, "column-0")
    selected = _rendered(session.statements[0])
    leased = _rendered(session.statements[1])
    check_condition(
        "领取查询跳过已锁行并排除死信",
        "SKIP LOCKED" in selected and "attempts <" in selected,
        actual=selected,
        expected="SELECT 包含 SKIP LOCKED 与 attempts 上限",
    )
    check_condition(
        "租约使用数据库时钟并约束 desired version",
        "timestampadd" in leased.lower() and "desired_version" in leased,
        actual=leased,
        expected="UPDATE 使用 timestampadd(now()) 且匹配 desired_version",
    )


async def test_ack_and_backoff_reject_stale_worker_generations() -> None:
    """确认和退避都必须匹配版本、操作与领取令牌。"""
    session = _RecordingSession(
        [
            _FakeResult(rowcount=0),
            _FakeResult(rowcount=0),
            _FakeResult(rowcount=0),
            _FakeResult(rowcount=0),
        ]
    )
    repository = MetadataIndexOutboxRepository(cast(AsyncSession, session))
    stale = _work("stale-worker-token".ljust(32, "x"))

    check_equal("迟到确认未命中", await repository.acknowledge(stale), False)
    check_equal(
        "迟到退避未命中",
        await repository.backoff(stale, "TimeoutError"),
        False,
    )
    for label, statement in zip(
        ("确认版本提升", "确认删除", "退避版本提升", "退避"),
        session.statements,
        strict=True,
    ):
        rendered = _rendered(statement)
        check_condition(
            f"{label}完整 CAS",
            all(
                field in rendered
                for field in ("operation", "desired_version", "lease_token")
            ),
            actual=rendered,
            expected="WHERE 同时匹配操作、版本和租约令牌",
        )


async def test_advance_progress_uses_full_authority_and_releases_lease() -> None:
    """字段进度只能由当前完整领取身份保存，并立即允许下一次领取。"""
    session = _RecordingSession([_FakeResult(rowcount=0)])
    repository = MetadataIndexOutboxRepository(cast(AsyncSession, session))

    check_equal(
        "迟到字段进度未命中",
        await repository.advance_progress(_work(), "column-2"),
        False,
    )

    rendered = _rendered(session.statements[0])
    check_condition(
        "字段进度完整 CAS 并释放租约",
        all(
            field in rendered
            for field in (
                "operation",
                "desired_version",
                "lease_token",
                "progress_column_id",
                "available_at=now()",
                "lease_token=%s",
                "lease_expires_at=%s",
            )
        ),
        actual=rendered,
        expected="UPDATE 匹配完整领取身份、保存字段游标并清空租约",
    )


async def test_lease_renewal_requires_current_unexpired_generation() -> None:
    """续租必须匹配完整身份且拒绝已经过期的 lease。"""
    session = _RecordingSession([_FakeResult(rowcount=1)])
    repository = MetadataIndexOutboxRepository(cast(AsyncSession, session))

    await repository.renew_lease(_work())

    rendered = _rendered(session.statements[0])
    check_condition(
        "续租完整 CAS",
        all(
            field in rendered
            for field in (
                "operation",
                "desired_version",
                "lease_token",
                "lease_expires_at > now()",
                "timestampadd",
            )
        ),
        actual=rendered,
        expected="UPDATE 匹配完整代次、未过期 lease 并使用数据库时钟续租",
    )


async def test_stale_write_preserves_an_existing_newer_generation() -> None:
    """迟到外部写入只在 outbox 缺失时补回，不得覆盖并发新版本。"""
    session = _RecordingSession([_FakeResult(rowcount=1)])
    repository = MetadataIndexOutboxRepository(cast(AsyncSession, session))

    await repository.restore_reconciliation(_work())

    check_equal("补回只执行一条原子语句", len(session.statements), 1)
    rendered = _rendered(session.statements[0])
    duplicate = rendered.split("ON DUPLICATE KEY UPDATE")[1]
    check_condition(
        "补回保留并发期望版本",
        MetadataIndexOperation.UPSERT.value in rendered
        and "desired_version = data_sync.metadata_index_outbox.desired_version"
        in duplicate,
        actual=duplicate,
        expected="冲突时保留既有 desired_version",
    )


async def test_enqueue_value_initializes_bounded_refresh_state() -> None:
    """首次 VALUES 期望必须从 SCAN 和独立频次代次开始。"""
    session = _RecordingSession()
    repository = MetadataIndexOutboxRepository(cast(AsyncSession, session))
    await repository.enqueue(
        [
            MetadataIndexDesired(
                target=MetadataIndexTarget.VALUES,
                object_kind=MetadataObjectKind.TABLE,
                object_id="table-1",
                operation=MetadataIndexOperation.REFRESH,
                desired_version="n" * 64,
                frequency_version="f" * 64,
            )
        ],
        debounce_seconds=5,
    )

    rendered = _rendered(session.statements[-1])
    check_condition(
        "初始化持久化状态机",
        all(
            field in rendered
            for field in ("frequency_version", "phase", "index_generation")
        ),
        actual=rendered,
        expected="VALUES INSERT 包含频次代次、SCAN phase 和索引代次",
    )


async def test_enqueue_active_value_records_latest_pending_version() -> None:
    """活跃刷新只合并最新 pending desired/frequency，不夺取当前 lease。"""
    session = _RecordingSession(
        [
            _FakeResult(
                [
                    {
                        "desired_version": "old",
                        "frequency_version": "old-frequency",
                        "phase": "publish",
                        "lease_token": "l" * 32,
                        "attempts": 0,
                    }
                ]
            )
        ]
    )
    repository = MetadataIndexOutboxRepository(cast(AsyncSession, session))

    await repository.enqueue(
        [
            MetadataIndexDesired(
                target=MetadataIndexTarget.VALUES,
                object_kind=MetadataObjectKind.TABLE,
                object_id="table-1",
                operation=MetadataIndexOperation.REFRESH,
                desired_version="new-version",
                frequency_version="new-frequency",
            )
        ]
    )

    rendered = _rendered(session.statements[-1])
    check_condition(
        "活跃版本写入双 pending 字段",
        "pending_desired_version" in rendered
        and "pending_frequency_version" in rendered
        and "lease_token" not in rendered.split(" SET ", 1)[-1],
        actual=rendered,
        expected="只更新 pending desired/frequency 和 available_at",
    )


async def test_restarting_old_frequency_generation_clears_stale_counts() -> None:
    """V1→V2→V1 回退重新 SCAN 前必须清空遗留的 V1 精确频次。"""
    session = _RecordingSession(
        [
            _FakeResult(
                [
                    {
                        "desired_version": "v2",
                        "frequency_version": "frequency-v2",
                        "phase": "complete",
                        "lease_token": None,
                        "attempts": 0,
                        "index_generation": "publication-generation",
                    }
                ]
            )
        ]
    )
    repository = MetadataIndexOutboxRepository(cast(AsyncSession, session))

    await repository.enqueue(
        [
            MetadataIndexDesired(
                target=MetadataIndexTarget.VALUES,
                object_kind=MetadataObjectKind.TABLE,
                object_id="table-1",
                operation=MetadataIndexOperation.REFRESH,
                desired_version="v1-again",
                frequency_version="frequency-v1",
            )
        ]
    )

    delete_sql = _rendered(session.statements[-2])
    update_sql = _rendered(session.statements[-1])
    check_condition(
        "回退代次先清空遗留频次",
        "DELETE FROM data_sync.metadata_value_frequency" in delete_sql
        and "frequency_version" in delete_sql,
        actual=delete_sql,
        expected="按 table_id 和 frequency_version 删除旧频次",
    )
    check_condition(
        "回退代次重新进入 SCAN",
        "phase" in update_sql and "last_primary_key" in update_sql,
        actual=update_sql,
        expected="清空游标并重新 SCAN",
    )


async def test_backoff_promotes_pending_version_before_dead_letter() -> None:
    """当前代次即将死信时必须提升已经等待的新代次。"""
    session = _RecordingSession()
    repository = MetadataIndexOutboxRepository(cast(AsyncSession, session))

    await repository.backoff(_work(), "remote_failure")

    rendered = _rendered(session.statements[0])
    check_condition(
        "待处理代次在当前代次死信前提升",
        all(
            fragment in rendered
            for fragment in (
                "pending_desired_version IS NOT NULL",
                "attempts + %s >= %s",
                "progress_column_id=%s",
                "pending_desired_version=%s",
                "desired_version=data_sync.metadata_index_outbox.pending_desired_version",
            )
        ),
        actual=rendered,
        expected="达到预算时原子提升 pending 版本并重置执行状态",
    )


async def test_same_frequency_pending_scan_finishes_into_requested_phase() -> None:
    """同频 pending 恰逢 SCAN 结束时必须进入调用方请求的下一阶段。"""
    session = _RecordingSession(
        [
            _FakeResult(
                [
                    {
                        "pending_desired_version": "next-version",
                        "pending_frequency_version": "frequency-v1",
                        "frequency_version": "frequency-v1",
                        "phase": MetadataValueRefreshPhase.SCAN.value,
                        "index_generation": "publication-generation",
                    }
                ]
            ),
            _FakeResult(),
        ]
    )
    repository = MetadataIndexOutboxRepository(cast(AsyncSession, session))
    item = ClaimedMetadataIndexWork(
        target=MetadataIndexTarget.VALUES,
        object_kind=MetadataObjectKind.TABLE,
        object_id="table-1",
        operation=MetadataIndexOperation.REFRESH,
        desired_version="current-version",
        frequency_version="frequency-v1",
        lease_token="a" * 32,
        phase=MetadataValueRefreshPhase.SCAN,
        index_generation="publication-generation",
    )

    await repository.advance_value_state(
        item,
        phase=MetadataValueRefreshPhase.SELECT_TOP_N,
    )

    rendered = _rendered(session.statements[-1])
    check_condition(
        "SCAN 完成边界保留请求阶段",
        "'phase': 'select_top_n'" in rendered
        and "'progress_column_id': None" in rendered
        and "'last_primary_key': None" in rendered,
        actual=rendered,
        expected="同频 pending 提升到 SELECT_TOP_N 并清空 SCAN 游标",
    )


async def test_same_frequency_pending_preserves_top_n_progress() -> None:
    """持续 CDC 的同频版本不得在 SELECT_TOP_N 中清空字段进度。"""
    session = _RecordingSession(
        [
            _FakeResult(
                [
                    {
                        "pending_desired_version": "next-version",
                        "pending_frequency_version": "frequency-v1",
                        "frequency_version": "frequency-v1",
                        "phase": MetadataValueRefreshPhase.SELECT_TOP_N.value,
                    }
                ]
            )
        ]
    )
    repository = MetadataIndexOutboxRepository(cast(AsyncSession, session))
    item = ClaimedMetadataIndexWork(
        target=MetadataIndexTarget.VALUES,
        object_kind=MetadataObjectKind.TABLE,
        object_id="table-1",
        operation=MetadataIndexOperation.REFRESH,
        desired_version="current-version",
        frequency_version="frequency-v1",
        lease_token="a" * 32,
        progress_column_id="column-7",
        phase=MetadataValueRefreshPhase.SELECT_TOP_N,
        index_generation="publication-generation",
    )

    promoted = await repository.promote_pending_value_state(item)

    check_equal("同频 Top-N 不提前提升", promoted, False)
    check_equal("同频 Top-N 不写状态", len(session.statements), 1)


async def test_authority_check_matches_full_claim_identity() -> None:
    """外部修改前必须用完整期望状态与租约身份复核执行权。"""
    session = _RecordingSession()
    repository = MetadataIndexOutboxRepository(cast(AsyncSession, session))

    check_equal("当前领取仍有执行权", await repository.is_authoritative(_work()), True)
    rendered = _rendered(session.statements[0])
    check_condition(
        "执行权复核使用完整 CAS",
        all(
            field in rendered
            for field in ("operation", "desired_version", "lease_token")
        ),
        actual=rendered,
        expected="SELECT 同时匹配操作、版本和租约令牌",
    )


async def test_snapshot_desired_states_cover_all_operations() -> None:
    """快照同时发布当前语义、删除墓碑与表级值刷新。"""
    schema = await parse_ddl(
        "metadata_index",
        "CREATE TABLE dim_product (id BIGINT PRIMARY KEY, name VARCHAR(64))",
    )
    states = semantic_desired_states(
        schema,
        semantic_for(schema, fact=False),
        [],
        removed_columns={"old-column"},
        removed_metrics={"old-metric"},
    )
    identities = {
        (item.target, item.object_kind, item.object_id, item.operation)
        for item in states
    }
    table_id = schema.tables[0].id
    check_condition(
        "当前表发布语义与值刷新",
        {
            (
                MetadataIndexTarget.SEMANTIC,
                MetadataObjectKind.TABLE,
                table_id,
                MetadataIndexOperation.UPSERT,
            ),
            (
                MetadataIndexTarget.VALUES,
                MetadataObjectKind.TABLE,
                table_id,
                MetadataIndexOperation.REFRESH,
            ),
        }
        <= identities,
        actual=identities,
        expected="表语义 UPSERT 与 VALUES REFRESH",
    )
    check_condition(
        "删除对象发布语义墓碑",
        {
            (
                MetadataIndexTarget.SEMANTIC,
                MetadataObjectKind.COLUMN,
                "old-column",
                MetadataIndexOperation.DELETE,
            ),
            (
                MetadataIndexTarget.SEMANTIC,
                MetadataObjectKind.METRIC,
                "old-metric",
                MetadataIndexOperation.DELETE,
            ),
        }
        <= identities,
        actual=identities,
        expected="列与指标 DELETE desired state",
    )


async def test_semantic_versions_cover_related_projection_context() -> None:
    """表和指标版本必须随关联字段语义变化。"""
    schema = await parse_ddl(
        "metadata_index",
        "CREATE TABLE fact_order (id BIGINT PRIMARY KEY, amount DECIMAL(10,2))",
    )
    metadata = semantic_for(schema, fact=True)
    metric = MetricMetadata(
        id="metric-1",
        name="订单金额",
        fact_table_id=schema.tables[0].id,
        definition="订单金额合计",
        relevant_column_ids=[schema.tables[0].columns[1].id],
        answer_question_ids=["q1"],
    )
    before = semantic_desired_states(schema, metadata, [metric])
    changed_columns = list(metadata.columns)
    changed_columns[1] = changed_columns[1].model_copy(
        update={"description": "含税订单金额"}
    )
    after = semantic_desired_states(
        schema,
        metadata.model_copy(update={"columns": changed_columns}),
        [metric],
    )
    before_versions = {
        (item.object_kind, item.object_id): item.desired_version
        for item in before
        if item.target == MetadataIndexTarget.SEMANTIC
    }
    after_versions = {
        (item.object_kind, item.object_id): item.desired_version
        for item in after
        if item.target == MetadataIndexTarget.SEMANTIC
    }
    check_condition(
        "表版本覆盖字段语义",
        before_versions[(MetadataObjectKind.TABLE, schema.tables[0].id)]
        != after_versions[(MetadataObjectKind.TABLE, schema.tables[0].id)],
        expected="字段描述变化生成新的表 desired_version",
    )
    check_condition(
        "指标版本覆盖关联字段语义",
        before_versions[(MetadataObjectKind.METRIC, metric.id)]
        != after_versions[(MetadataObjectKind.METRIC, metric.id)],
        expected="字段描述变化生成新的指标 desired_version",
    )
