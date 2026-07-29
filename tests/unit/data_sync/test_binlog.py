"""MySQL ROW Binlog 事件解码检查。"""

from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pymysqlreplication.row_event import (
    DeleteRowsEvent,
    UpdateRowsEvent,
    WriteRowsEvent,
)
from tests.helpers.checks import check_equal, check_exception, fail_check

from data_agent.data_sync.binlog import MySQLSourceClient, decode_rows_event
from data_agent.data_sync.models import BinlogCoordinate, RowOperation


def _event(event_type: type[Any], rows: list[dict[str, object]]) -> object:
    """构造不依赖网络和 Binlog packet 的第三方行事件。"""
    event = object.__new__(event_type)
    setattr(event, "schema", "business")
    setattr(event, "table", "fact_order")
    setattr(event, "_RowsEvent__rows", rows)
    setattr(event, "columns", [])
    return event


def test_decode_json_scalar_uses_column_metadata() -> None:
    """Binlog JSON 列的顶层标量不会退化为普通 SQL 标量。"""
    from pymysql.constants import FIELD_TYPE

    event = _event(WriteRowsEvent, [{"values": {"id": 1, "payload": None}}])
    setattr(event, "columns", [SimpleNamespace(name="payload", type=FIELD_TYPE.JSON)])
    decoded = decode_rows_event(
        event,
        source="source_demo",
        coordinate=BinlogCoordinate(file="mysql-bin.000001", position=120, row_index=0),
    )
    check_equal(
        "JSON null 使用独立标签",
        decoded[0].after,
        {"id": 1, "payload": {"$json": "null"}},
    )


@pytest.mark.parametrize(
    ("event", "operation", "before", "after"),
    [
        (
            _event(WriteRowsEvent, [{"values": {"id": 1, "amount": Decimal("2.5")}}]),
            RowOperation.INSERT,
            None,
            {"id": 1, "amount": {"$decimal": "2.5"}},
        ),
        (
            _event(
                UpdateRowsEvent,
                [
                    {
                        "before_values": {"id": 1, "amount": 2},
                        "after_values": {"id": 1, "amount": 3},
                    }
                ],
            ),
            RowOperation.UPDATE,
            {"id": 1, "amount": 2},
            {"id": 1, "amount": 3},
        ),
        (
            _event(DeleteRowsEvent, [{"values": {"id": 1, "amount": 3}}]),
            RowOperation.DELETE,
            {"id": 1, "amount": 3},
            None,
        ),
        (
            _event(
                WriteRowsEvent,
                [{"values": {"id": 1, "payload": {"items": [1, None]}}}],
            ),
            RowOperation.INSERT,
            None,
            {"id": 1, "payload": {"$json": '{"items":[1,null]}'}},
        ),
    ],
)
def test_decode_rows_event(
    event: object,
    operation: RowOperation,
    before: object,
    after: object,
) -> None:
    """三种 ROW 事件均投影为可持久化的单行契约。"""
    decoded = decode_rows_event(
        event,
        source="source_demo",
        coordinate=BinlogCoordinate(
            file="mysql-bin.000001",
            position=120,
            row_index=0,
        ),
    )
    check_equal("单行事件数量", len(decoded), 1)
    check_equal("事件操作", decoded[0].operation, operation)
    check_equal("事件前镜像", decoded[0].before, before)
    check_equal("事件后镜像", decoded[0].after, after)


def test_decode_rows_event_rejects_unknown_event() -> None:
    """未知第三方事件不会被静默当作写入。"""
    try:
        decode_rows_event(
            object(),
            source="source_demo",
            coordinate=BinlogCoordinate(
                file="mysql-bin.000001",
                position=120,
                row_index=0,
            ),
        )
    except Exception as error:
        check_exception("未知 Binlog 事件被拒绝", error, TypeError)
    else:
        fail_check(
            "未知 Binlog 事件被拒绝",
            actual="未抛出异常",
            expected="TypeError",
        )


def test_capture_enforces_limit_inside_rows_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单个事务跨多个 RowsEvent 时按行上限分次恢复。"""
    first = _event(
        WriteRowsEvent,
        [{"values": {"id": 1}}, {"values": {"id": 2}}],
    )
    second = _event(
        WriteRowsEvent,
        [{"values": {"id": 3}}, {"values": {"id": 4}}],
    )
    setattr(first, "packet", SimpleNamespace(log_pos=120, event_size=20))
    setattr(second, "packet", SimpleNamespace(log_pos=140, event_size=20))

    class FakeStream:
        """按请求位点重放测试行事件。"""

        def __init__(self, **kwargs: object) -> None:
            start = int(cast(Any, kwargs["log_pos"]))
            self._events = [first, second]
            self.log_file = "mysql-bin.000001"
            self.log_pos = start

        def __iter__(self) -> object:
            for event in self._events:
                self.log_pos = int(event.packet.log_pos)  # type: ignore[attr-defined]
                yield event

        def close(self) -> None:
            return None

    monkeypatch.setattr("data_agent.data_sync.binlog.BinLogStreamReader", FakeStream)
    client = object.__new__(MySQLSourceClient)
    dynamic_client: Any = client
    dynamic_client.name = "source_demo"
    dynamic_client._settings = SimpleNamespace(server_id=101)
    dynamic_client._url = SimpleNamespace(
        host="localhost",
        port=3306,
        username="root",
        password="",
        database="business",
        query={},
    )
    dynamic_client._connect_timeout_seconds = 1
    dynamic_client._read_timeout_seconds = 1

    first_batch = client._capture_sync(
        source_schema="business",
        source_table="fact_order",
        start=BinlogCoordinate(file="mysql-bin.000001", position=4, row_index=0),
        limit=3,
    )
    second_batch = client._capture_sync(
        source_schema="business",
        source_table="fact_order",
        start=first_batch.tail,
        limit=3,
    )

    check_equal("首批严格受限", len(first_batch.events), 3)
    check_equal("中间位点记录已消费行", first_batch.tail.row_index, 3)
    check_equal("续传仅返回剩余行", len(second_batch.events), 1)
    check_equal("续传行主键", second_batch.events[0].after, {"id": 4})
