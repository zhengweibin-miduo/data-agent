"""MySQL ROW Binlog 事件解码检查。"""

import inspect
from decimal import Decimal
from types import MethodType, SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from pymysql.constants import FIELD_TYPE
from pymysqlreplication.event import XidEvent
from pymysqlreplication.row_event import (
    DeleteRowsEvent,
    RowsEvent,
    UpdateRowsEvent,
    WriteRowsEvent,
)
from sqlalchemy.engine import make_url
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)

from data_agent.data_sync.binlog import (
    _ROWS_VALUE_DECODER_NAME,
    MySQLSourceClient,
    _replication_connection_settings,
    _uses_partial_json,
    decode_rows_event,
)
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


def test_locked_dependency_decoder_signature_is_compatible() -> None:
    """锁定依赖必须保留实例适配器依赖的完整私有解码签名。"""
    decoder = getattr(RowsEvent, _ROWS_VALUE_DECODER_NAME)
    check_equal(
        "mysql-replication 私有解码参数",
        tuple(inspect.signature(decoder).parameters),
        (
            "self",
            "column",
            "null_bitmap",
            "null_bitmap_index",
            "is_partial",
            "cols_bitmap",
            "unsigned",
            "i",
        ),
    )


def test_partial_json_capability_is_rejected_case_insensitively() -> None:
    """启动能力门禁必须识别组合配置中的 PARTIAL_JSON。"""
    check_equal("空行值选项受支持", _uses_partial_json(""), False)
    check_equal(
        "PARTIAL_JSON 被拒绝",
        _uses_partial_json("other, partial_json"),
        True,
    )


@pytest.mark.parametrize(
    ("event_type", "sql_null_images", "before", "after"),
    [
        (WriteRowsEvent, (True,), None, {"payload": None}),
        (
            WriteRowsEvent,
            (False,),
            None,
            {"payload": {"$json": "null"}},
        ),
        (
            UpdateRowsEvent,
            (True, False),
            {"payload": None},
            {"payload": {"$json": "null"}},
        ),
        (DeleteRowsEvent, (True,), {"payload": None}, None),
    ],
)
def test_lazy_rows_adapter_preserves_json_sql_null_source(
    event_type: type[Any],
    sql_null_images: tuple[bool, ...],
    before: object,
    after: object,
) -> None:
    """三种 FULL ROW 事件首次惰性解码时区分 SQL NULL 与 JSON null。"""
    fetched: list[bool] = []
    event = object.__new__(event_type)
    setattr(event, "schema", "business")
    setattr(event, "table", "fact_order")
    setattr(event, "_RowsEvent__rows", None)
    column = SimpleNamespace(name="payload", type=FIELD_TYPE.JSON, length_size=1)
    setattr(event, "columns", [column])
    setattr(event, "table_id", 1)
    setattr(
        event,
        "table_map",
        {1: SimpleNamespace(columns=[SimpleNamespace(name="payload", unsigned=False)])},
    )
    setattr(event, "packet", SimpleNamespace(read_binary_json=Mock(return_value=None)))

    def fetch_rows(self: RowsEvent) -> None:
        fetched.append(_ROWS_VALUE_DECODER_NAME in vars(self))
        decoder = getattr(self, _ROWS_VALUE_DECODER_NAME)
        images = [
            decoder(
                column,
                b"\x01" if sql_null else b"\x00",
                0,
                False,
                b"\x01",
                False,
                0,
            )
            for sql_null in sql_null_images
        ]
        if isinstance(self, UpdateRowsEvent):
            rows = [
                {
                    "before_values": {"payload": images[0]},
                    "after_values": {"payload": images[1]},
                }
            ]
        else:
            rows = [{"values": {"payload": images[0]}}]
        setattr(self, "_RowsEvent__rows", rows)

    setattr(event, "_fetch_rows", MethodType(fetch_rows, event))
    untouched = object.__new__(event_type)

    decoded = decode_rows_event(
        event,
        source="source_demo",
        coordinate=BinlogCoordinate(file="mysql-bin.000001", position=120, row_index=0),
    )

    check_equal("适配器在首次 rows 读取前安装", fetched, [True])
    check_condition(
        "适配器仅安装在目标事件实例",
        _ROWS_VALUE_DECODER_NAME in vars(event)
        and _ROWS_VALUE_DECODER_NAME not in vars(untouched),
    )
    check_equal("JSON 空值事件前镜像", decoded[0].before, before)
    check_equal("JSON 空值事件后镜像", decoded[0].after, after)


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


def test_replication_connection_projects_tls_settings() -> None:
    """源 URL 的 CA、客户端证书和校验开关会传给复制连接。"""
    url = make_url(
        "mysql+asyncmy://user:secret@mysql/business"
        "?ssl_ca=%2Fcerts%2Fca.pem&ssl_cert=%2Fcerts%2Fclient.pem"
        "&ssl_key=%2Fcerts%2Fclient.key&ssl_verify_cert=true"
        "&ssl_verify_identity=1"
    )

    settings = _replication_connection_settings(
        url, connect_timeout_seconds=3, read_timeout_seconds=5
    )

    check_equal("复制连接使用 CA", settings["ssl_ca"], "/certs/ca.pem")
    check_equal("复制连接使用客户端证书", settings["ssl_cert"], "/certs/client.pem")
    check_equal("复制连接使用客户端私钥", settings["ssl_key"], "/certs/client.key")
    check_equal("复制连接校验证书", settings["ssl_verify_cert"], True)
    check_equal("复制连接校验主机名", settings["ssl_verify_identity"], True)


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


def test_capture_advances_replay_base_after_committed_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨过 XID 后续传只重放当前未完成事务。"""
    first = _event(WriteRowsEvent, [{"values": {"id": 1}}])
    second = _event(
        WriteRowsEvent,
        [{"values": {"id": 2}}, {"values": {"id": 3}}],
    )
    setattr(first, "packet", SimpleNamespace(log_pos=120, event_size=20))
    setattr(second, "packet", SimpleNamespace(log_pos=160, event_size=20))
    xid = object.__new__(XidEvent)
    starts: list[int] = []

    class FakeStream:
        """记录每轮起点并模拟两个事务。"""

        def __init__(self, **kwargs: object) -> None:
            self.log_file = "mysql-bin.000001"
            self.log_pos = int(cast(Any, kwargs["log_pos"]))
            starts.append(self.log_pos)

        def __iter__(self) -> object:
            for event, position in ((first, 120), (xid, 140), (second, 160)):
                if position <= self.log_pos:
                    continue
                self.log_pos = position
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

    batch = client._capture_sync(
        source_schema="business",
        source_table="fact_order",
        start=BinlogCoordinate(file="mysql-bin.000001", position=4, row_index=0),
        limit=2,
    )
    resumed = client._capture_sync(
        source_schema="business",
        source_table="fact_order",
        start=batch.tail,
        limit=1,
    )

    check_equal("续传基点推进到最近 XID", batch.tail.position, 140)
    check_equal("当前事务只记录已消费行", batch.tail.row_index, 1)
    check_equal("下一轮从最近 XID 开始", starts, [4, 140])
    check_equal("续传返回当前事务剩余行", resumed.events[0].after, {"id": 3})
