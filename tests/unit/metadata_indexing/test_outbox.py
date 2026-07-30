"""元数据索引 desired state 与租约结算契约。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ClauseElement
from tests.helpers.checks import check_condition, check_equal
from tests.helpers.factories import semantic_for

from data_agent.data_sync.models import DesiredColumn, DesiredSyncTable
from data_agent.ddl_metadata.parsing import parse_ddl
from data_agent.metadata_indexing.desired import (
    semantic_desired_states,
    shared_value_refresh_states,
)
from data_agent.metadata_indexing.models import (
    ClaimedMetadataIndexWork,
    MetadataIndexDesired,
    MetadataIndexOperation,
    MetadataIndexTarget,
    MetadataObjectKind,
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
    session = _RecordingSession([_FakeResult([row]), _FakeResult()])
    repository = MetadataIndexOutboxRepository(cast(AsyncSession, session))

    claimed = await repository.claim(1)

    check_equal("领取一条工作", len(claimed), 1)
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
    session = _RecordingSession([_FakeResult(rowcount=0), _FakeResult(rowcount=0)])
    repository = MetadataIndexOutboxRepository(cast(AsyncSession, session))
    stale = _work("stale-worker-token".ljust(32, "x"))

    check_equal("迟到确认未命中", await repository.acknowledge(stale), False)
    check_equal(
        "迟到退避未命中",
        await repository.backoff(stale, "TimeoutError"),
        False,
    )
    for label, statement in zip(("确认", "退避"), session.statements, strict=True):
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


async def test_enqueue_new_version_invalidates_lease_and_retry_state() -> None:
    """新 desired version 覆盖时必须清理旧 worker 的执行权。"""
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
            )
        ],
        debounce_seconds=5,
    )

    rendered = _rendered(session.statements[0])
    duplicate = rendered.split("ON DUPLICATE KEY UPDATE")[1]
    check_condition(
        "新版本重置租约与失败状态",
        all(
            field in duplicate
            for field in (
                "attempts",
                "available_at",
                "lease_token",
                "lease_expires_at",
                "last_error_type",
            )
        ),
        actual=duplicate,
        expected="冲突更新覆盖完整执行状态",
    )
    check_condition(
        "连续变更保留最早刷新期限",
        "least(data_sync.metadata_index_outbox.available_at" in duplicate.lower(),
        actual=duplicate,
        expected="available_at 使用既有期限与新 debounce 期限的较早值",
    )


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
