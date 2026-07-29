"""数据同步基线分批清理检查。"""

from unittest.mock import AsyncMock

import pytest
from tests.helpers.checks import check_equal

from data_agent.data_sync import backfill
from data_agent.data_sync.models import DesiredColumn, DesiredSyncTable, SyncPhase
from data_agent.data_sync.repository import ClaimedSyncTask


def _task() -> ClaimedSyncTask:
    """构造基线重建任务。"""
    desired = DesiredSyncTable(
        source="local",
        source_schema="business",
        source_table="fact_order",
        target_table="fact_order",
        columns=[DesiredColumn(id="id", name="id", data_type="BIGINT", nullable=False)],
        primary_key=["id"],
        schema_fingerprint="a" * 64,
        metric_dependency_column_ids=[],
    )
    return ClaimedSyncTask(
        id=1,
        desired=desired,
        desired_hash=desired.desired_hash(),
        phase=SyncPhase.BUFFERING,
        lease_token="a" * 32,
        attempts=0,
        snapshot=None,
        captured=None,
        applied=None,
        last_backfill_key=None,
    )


async def test_reset_source_rows_is_bounded_and_resumable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """大 generation 清理每次只处理一批并通过墓碑续传。"""
    repository = AsyncMock()
    repository.source_key_documents.side_effect = [
        ['{"id":1}', '{"id":2}'],
        ['{"id":3}'],
    ]
    monkeypatch.setattr(backfill, "DataSyncRepository", lambda session: repository)
    session = AsyncMock()

    first_complete = await backfill.reset_source_rows(
        session, _task(), dw_database="dw", limit=2
    )
    second_complete = await backfill.reset_source_rows(
        session, _task(), dw_database="dw", limit=2
    )

    check_equal("满批后需要续传", first_complete, False)
    check_equal("末批后完成", second_complete, True)
    check_equal("每次查询使用有界 limit", repository.source_key_documents.call_count, 2)
    check_equal(
        "每批归属都持久化墓碑", repository.tombstone_source_key_owners.call_count, 2
    )
