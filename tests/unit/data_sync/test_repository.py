"""数据同步控制面仓储状态转换检查。"""

from unittest.mock import AsyncMock, Mock

from tests.helpers.checks import check_equal

from data_agent.data_sync.models import DesiredColumn, DesiredSyncTable, SyncPhase
from data_agent.data_sync.repository import ClaimedSyncTask, DataSyncRepository


def _streaming_task() -> ClaimedSyncTask:
    """构造已进入实时阶段且持有租约的任务。"""
    desired = DesiredSyncTable(
        source="local",
        source_schema="business",
        source_table="fact_order",
        target_table="fact_order",
        columns=[
            DesiredColumn(
                id="order_id",
                name="order_id",
                data_type="BIGINT",
                nullable=False,
            )
        ],
        primary_key=["order_id"],
        schema_fingerprint="a" * 64,
        metric_dependency_column_ids=[],
    )
    return ClaimedSyncTask(
        id=1,
        desired=desired,
        desired_hash=desired.desired_hash(),
        phase=SyncPhase.STREAMING,
        lease_token="a" * 32,
        attempts=0,
        snapshot=None,
        captured=None,
        applied=None,
        last_backfill_key=None,
    )


async def test_streaming_retry_returns_to_non_ready_replay_phase() -> None:
    """实时捕获失败后在下次成功追平前不得继续声明就绪。"""
    attempts_result = Mock()
    attempts_result.scalar_one_or_none.return_value = 0
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[attempts_result, Mock()])

    phase = await DataSyncRepository(session).retry_failure(
        _streaming_task(),
        error_type="source_transport_error",
        retry_base_seconds=1,
        retry_max_seconds=60,
        max_attempts=3,
    )

    check_equal("失败退避撤销实时就绪阶段", phase, SyncPhase.REPLAYING)


async def test_readiness_uses_dedicated_worker_heartbeat() -> None:
    """控制面 updated_at 不得被解释为 CDC worker 活性。"""
    session = AsyncMock()
    result = Mock()
    result.__iter__ = Mock(return_value=iter([(SyncPhase.STREAMING.value, False)]))
    session.execute.return_value = result

    phases = await DataSyncRepository(session).read_readiness_phases(
        target_table="fact_order", source="local", heartbeat_timeout_seconds=30
    )

    check_equal("独立心跳过期关闭门禁", phases, [(SyncPhase.STREAMING, False)])
    selected = list(session.execute.await_args.args[0].selected_columns)
    check_equal("查询使用 worker 心跳", "worker_heartbeat_at" in str(selected[1]), True)
    check_equal("查询不复用控制面时间", "updated_at" in str(selected[1]), False)


async def test_peer_generation_change_requeues_paused_task() -> None:
    """共享目标的 peer 契约变化后应重新校验已暂停任务。"""
    session = AsyncMock()
    session.scalar.side_effect = [None, None, 1]
    repository = DataSyncRepository(session)

    await repository.upsert_desired([_streaming_task().desired])

    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    assert any(
        "phase" in statement and "target_table" in statement for statement in statements
    )
