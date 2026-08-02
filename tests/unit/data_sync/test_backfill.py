"""数据同步基线分批清理检查。"""

from unittest.mock import AsyncMock

import pytest
from tests.helpers.checks import check_condition, check_equal

from data_agent.data_sync import backfill
from data_agent.data_sync.models import (
    BinlogCoordinate,
    DesiredColumn,
    DesiredSyncTable,
    RowOperation,
    SyncPhase,
    SyncRowEvent,
)
from data_agent.data_sync.repository import BufferedSyncEvent, ClaimedSyncTask
from data_agent.ddl_metadata.meta_projection.application.value_input import (
    MaterializedRowsChanged,
    MaterializedTableRef,
)


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


@pytest.fixture
def value_refresh(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """隔离并记录与 DW 写入同事务触发的索引刷新。"""
    refresh = AsyncMock()
    prepared = AsyncMock()
    prepared.needs_before_rows = False
    prepared.apply = refresh
    participant = AsyncMock()
    participant.prepare.return_value = prepared
    monkeypatch.setattr(
        backfill,
        "MySQLValueProjectionParticipant",
        lambda session, desired: participant,
    )
    return refresh


async def test_reset_source_rows_is_bounded_and_resumable(
    monkeypatch: pytest.MonkeyPatch,
    value_refresh: AsyncMock,
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
    check_equal("每批 DW 删除使用一条语句", session.execute.await_count, 2)
    check_equal("每个非空清理批次触发值刷新", value_refresh.await_count, 2)


def test_desired_values_normalizes_mysql_set_values() -> None:
    """历史回填将多成员和空 MySQL SET 转为稳定的可绑定文本。"""
    desired = _task().desired.model_copy(
        update={
            "columns": [
                *_task().desired.columns,
                DesiredColumn(
                    id="labels",
                    name="labels",
                    data_type="SET('alpha','beta')",
                    nullable=False,
                ),
            ]
        }
    )

    populated = backfill._desired_values(
        desired, {"id": 1, "labels": {"beta", "alpha"}}
    )
    empty = backfill._desired_values(desired, {"id": 2, "labels": set()})

    check_equal("多成员 SET 稳定排序", populated["labels"], "alpha,beta")
    check_equal("空 SET 绑定为空字符串", empty["labels"], "")


def test_set_primary_key_has_one_identity_before_and_after_binding() -> None:
    """SET 主键的驱动集合和 DW 文本表示必须得到相同归属。"""
    desired = _task().desired.model_copy(
        update={
            "columns": [
                DesiredColumn(
                    id="id",
                    name="id",
                    data_type="SET('alpha','beta')",
                    nullable=False,
                )
            ]
        }
    )

    source_identity = backfill.primary_key_identity(desired, {"id": {"beta", "alpha"}})
    target_identity = backfill.primary_key_identity(desired, {"id": "alpha,beta"})

    check_equal("SET 主键归属编码一致", source_identity, target_identity)


async def test_apply_backfill_batch_claims_ownership_in_one_batch(
    monkeypatch: pytest.MonkeyPatch,
    value_refresh: AsyncMock,
) -> None:
    """历史回填按块领取 ownership，而不是逐行执行数据库往返。"""
    repository = AsyncMock()
    repository.claim_key_owners.return_value = None
    repository.record_backfill_cursor.return_value = True
    monkeypatch.setattr(backfill, "DataSyncRepository", lambda session: repository)
    session = AsyncMock()
    task = _task()

    await backfill.apply_backfill_batch(
        session,
        task,
        [{"id": 1}, {"id": 2}, {"id": 3}],
        dw_database="dw",
    )

    repository.claim_key_owners.assert_awaited_once()
    check_equal(
        "批量归属数量",
        len(repository.claim_key_owners.call_args.kwargs["identities"]),
        3,
    )
    repository.claim_key_owner.assert_not_awaited()
    value_refresh.assert_awaited_once()
    assert value_refresh.await_args is not None
    change = value_refresh.await_args.args[0]
    assert change.checkpoint == {"backfill_key": [3]}


async def test_backfill_uses_transaction_scoped_value_projection_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回填通过中立参与者在 DW DML 前 prepare，并在同一调用内提交变化。"""
    events: list[object] = []

    class PreparedProjection:
        """记录公共输入收到的物化行变化。"""

        needs_before_rows = False

        async def apply(self, change: MaterializedRowsChanged) -> None:
            events.append(change)

    class ProjectionParticipant:
        """记录 prepare 相对于 DW 写入的顺序。"""

        async def prepare(self, table: MaterializedTableRef) -> PreparedProjection:
            events.append(table)
            return PreparedProjection()

    repository = AsyncMock()
    repository.claim_key_owners.return_value = None
    repository.record_backfill_cursor.return_value = True
    monkeypatch.setattr(backfill, "DataSyncRepository", lambda session: repository)
    session = AsyncMock()
    session.execute.side_effect = lambda statement: events.append("dw_dml")
    task = _task()

    await backfill.apply_backfill_batch(
        session,
        task,
        [{"id": 1}],
        dw_database="dw",
        value_projection=ProjectionParticipant(),
    )

    assert isinstance(events[0], MaterializedTableRef)
    assert events[1] == "dw_dml"
    change = events[2]
    assert isinstance(change, MaterializedRowsChanged)
    assert change.before_rows == ()
    assert change.after_rows == ({"id": 1},)
    assert change.checkpoint == {"backfill_key": [1]}


def test_backfill_rows_are_chunked_by_encoded_payload_bytes() -> None:
    """大型回填批次按参数字节预算拆分，且保持全部行顺序。"""
    rows = [{"id": 1, "payload": "a" * 30}, {"id": 2, "payload": "b" * 30}]

    chunks = backfill._chunk_rows_by_payload(rows, byte_limit=60)

    check_equal("回填载荷拆成两块", [len(chunk) for chunk in chunks], [1, 1])
    check_equal(
        "拆分不改变行顺序",
        [row["id"] for chunk in chunks for row in chunk],
        [1, 2],
    )


def test_backfill_rejects_one_row_over_payload_budget() -> None:
    """单行超过写入预算时给出明确失败，而不是构造超包语句。"""
    with pytest.raises(ValueError, match="单行回填数据"):
        backfill._chunk_rows_by_payload(
            [{"id": 1, "payload": "x" * 100}], byte_limit=20
        )


@pytest.mark.parametrize("operation", [RowOperation.DELETE, RowOperation.UPDATE])
async def test_missing_old_key_does_not_claim_ownership(
    monkeypatch: pytest.MonkeyPatch,
    operation: RowOperation,
    value_refresh: AsyncMock,
) -> None:
    """扫描前已删除或迁移的旧键不得被删除事件抢占归属。"""
    repository = AsyncMock()
    repository.tombstone_key_owner.return_value = False
    repository.claim_key_owner.return_value = None
    repository.acknowledge_event.return_value = True
    repository.advance_applied_coordinate.return_value = True
    monkeypatch.setattr(backfill, "DataSyncRepository", lambda session: repository)
    session = AsyncMock()
    coordinate = BinlogCoordinate(file="mysql-bin.000001", position=120, row_index=0)
    event = SyncRowEvent(
        source="local",
        source_schema="business",
        source_table="fact_order",
        coordinate=coordinate,
        operation=operation,
        before={"id": 1},
        after={"id": 2} if operation == RowOperation.UPDATE else None,
    )

    await backfill.apply_buffered_event(
        session,
        _task(),
        BufferedSyncEvent(id=1, event=event),
        dw_database="dw",
    )

    if operation == RowOperation.DELETE:
        repository.claim_key_owner.assert_not_awaited()
        session.execute.assert_not_awaited()
    else:
        repository.claim_key_owner.assert_awaited_once()
        check_equal("主键迁移只写入新键", session.execute.await_count, 1)
    value_refresh.assert_awaited_once()
    assert value_refresh.await_args is not None
    change = value_refresh.await_args.args[0]
    assert change.checkpoint == {
        "coordinate": coordinate.model_dump(mode="json")
    }


async def test_buffered_insert_uses_current_dw_row_as_frequency_before_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回填已写入事件后镜像时，缓冲 INSERT 不得重复累计频次。"""
    repository = AsyncMock()
    repository.claim_key_owner.return_value = None
    repository.acknowledge_event.return_value = True
    repository.advance_applied_coordinate.return_value = True
    monkeypatch.setattr(backfill, "DataSyncRepository", lambda session: repository)
    monkeypatch.setattr(
        backfill, "_read_target_rows", AsyncMock(return_value=[{"id": 1}])
    )
    apply_changes = AsyncMock()
    prepared = AsyncMock()
    prepared.needs_before_rows = True
    prepared.apply = apply_changes
    participant = AsyncMock()
    participant.prepare.return_value = prepared
    monkeypatch.setattr(
        backfill,
        "MySQLValueProjectionParticipant",
        lambda session, desired: participant,
    )
    coordinate = BinlogCoordinate(file="mysql-bin.000001", position=121, row_index=0)
    event = SyncRowEvent(
        source="local",
        source_schema="business",
        source_table="fact_order",
        coordinate=coordinate,
        operation=RowOperation.INSERT,
        before=None,
        after={"id": 1},
    )

    await backfill.apply_buffered_event(
        AsyncMock(),
        _task(),
        BufferedSyncEvent(id=2, event=event),
        dw_database="dw",
    )

    apply_changes.assert_awaited_once()
    call = apply_changes.await_args
    check_condition("频次变化调用参数存在", call is not None)
    if call is None:
        return
    change = call.args[0]
    check_equal("当前 DW 镜像作为扣减端", change.before_rows, ({"id": 1},))
    check_equal("事件后镜像作为增加端", change.after_rows, ({"id": 1},))
