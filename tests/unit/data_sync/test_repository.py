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
