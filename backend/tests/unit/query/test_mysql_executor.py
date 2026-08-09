"""只读 MySQL 执行器的连接契约测试。"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

import query.adapters.mysql as mysql_adapter
from query.adapters.mysql import MySQLQueryExecutor


class _Result:
    def __init__(self) -> None:
        self.partition_sizes: list[int] = []

    def keys(self) -> list[str]:
        return ["payload"]

    def partitions(self, size: int):  # type: ignore[no-untyped-def]
        self.partition_sizes.append(size)

        async def _rows():  # type: ignore[no-untyped-def]
            yield [("x",)]

        return _rows()

    async def close(self) -> None:
        return None


class _AdaptiveResult(_Result):
    def __init__(self, rows: list[tuple[str]]) -> None:
        super().__init__()
        self.rows = rows
        self.offset = 0

    async def fetchmany(self, size: int) -> list[tuple[str]]:
        self.partition_sizes.append(size)
        batch = self.rows[self.offset : self.offset + size]
        self.offset += len(batch)
        return batch


class _Connection:
    def __init__(self, result: _Result) -> None:
        self.result = result

    async def start(self) -> None:
        return None

    async def exec_driver_sql(self, _sql: str) -> None:
        return None

    async def stream(self, _statement: object, _params: object) -> _Result:
        return self.result

    async def execute(self, _statement: object, _params: object) -> None:
        return None

    async def rollback(self) -> None:
        return None

    def in_transaction(self) -> bool:
        return False

    async def close(self) -> None:
        return None

    async def scalar(self, _statement: object, _params: object) -> int:
        return 1

    async def invalidate(self) -> None:
        return None

    async def __aenter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


async def test_query_executor_fetches_one_row_before_byte_validation() -> None:
    """驱动不能在逐行字节门禁前物化多行结果。"""
    executor = MySQLQueryExecutor(
        "mysql+asyncmy://query:secret@localhost/dw",
        timeout_seconds=10,
        fetch_batch_rows=500,
        max_batch_bytes=2048,
    )
    result = _Result()
    executor._engine = Mock(connect=Mock(return_value=_Connection(result)))

    query = Mock(sql="SELECT payload FROM dw.items", params={})
    batches = [batch async for batch in executor.execute(query)]

    assert result.partition_sizes == [1]
    assert batches[0].rows == [["x"]]


async def test_generation_lock_and_query_share_one_owner_connection() -> None:
    """锁 owner 断线必须与最终 EXPLAIN、SELECT 使用同一故障域。"""
    executor = MySQLQueryExecutor(
        "mysql+asyncmy://query:secret@localhost/dw",
        timeout_seconds=10,
        fetch_batch_rows=500,
        max_batch_bytes=2048,
    )
    connection = _Connection(_Result())
    connect = Mock(return_value=connection)
    executor._engine = Mock(connect=connect)
    query = Mock(sql="SELECT payload FROM dw.items", params={})

    async with executor.hold_generation(("generation:items",), 1):
        await executor.explain(query)
        _ = [batch async for batch in executor.execute(query)]

    connect.assert_called_once_with()


@pytest.mark.parametrize("failure_point", ["start", "initialize"])
@pytest.mark.parametrize(
    "failure",
    [RuntimeError("database failed"), asyncio.CancelledError("client cancelled")],
)
async def test_generation_connection_closes_when_initialization_fails(
    failure_point: str, failure: BaseException
) -> None:
    """Generation 连接初始化失败或取消后必须立即归还连接池。"""
    executor = MySQLQueryExecutor(
        "mysql+asyncmy://query:secret@localhost/dw",
        timeout_seconds=10,
        fetch_batch_rows=500,
        max_batch_bytes=2048,
    )
    connection = _Connection(_Result())
    connection.start = AsyncMock()  # type: ignore[method-assign]
    connection.exec_driver_sql = AsyncMock()  # type: ignore[method-assign]
    connection.close = AsyncMock()  # type: ignore[method-assign]
    if failure_point == "start":
        connection.start.side_effect = failure
    else:
        connection.exec_driver_sql.side_effect = failure
    executor._engine = Mock(connect=Mock(return_value=connection))

    with pytest.raises(type(failure), match=str(failure)):
        async with executor.hold_generation(("generation:items",), 1):
            pass

    connection.close.assert_awaited_once_with()


async def test_generation_connection_is_invalidated_when_release_returns_false(
) -> None:
    """Locking Service 无法证明释放成功时不得把 owner session 归还池。"""
    executor = MySQLQueryExecutor(
        "mysql+asyncmy://query:secret@localhost/dw",
        timeout_seconds=10,
        fetch_batch_rows=500,
        max_batch_bytes=2048,
    )
    connection = _Connection(_Result())
    connection.scalar = AsyncMock(side_effect=[1, 0])  # type: ignore[method-assign]
    connection.invalidate = AsyncMock()  # type: ignore[method-assign]
    executor._engine = Mock(connect=Mock(return_value=connection))

    async with executor.hold_generation(("generation:items",), 1):
        pass

    connection.invalidate.assert_awaited_once_with()


async def test_query_executor_expands_driver_reads_after_row_width_is_known() -> None:
    """首行通过字节门禁后，大结果应恢复配置的批量游标读取。"""
    executor = MySQLQueryExecutor(
        "mysql+asyncmy://query:secret@localhost/dw",
        timeout_seconds=10,
        fetch_batch_rows=4,
        max_batch_bytes=1024 * 1024,
    )
    result = _AdaptiveResult([("x",)] * 9)
    executor._engine = Mock(connect=Mock(return_value=_Connection(result)))

    query = Mock(sql="SELECT payload FROM dw.items", params={})
    batches = [batch async for batch in executor.execute(query)]

    assert result.partition_sizes == [1, 2, 4, 4, 4]
    assert sum(len(batch.rows) for batch in batches) == 9


async def test_query_executor_grows_driver_reads_conservatively() -> None:
    """窄首行不能让下一次读取直接放大到配置上限。"""
    executor = MySQLQueryExecutor(
        "mysql+asyncmy://query:secret@localhost/dw",
        timeout_seconds=10,
        fetch_batch_rows=500,
        max_batch_bytes=1024 * 1024,
    )
    result = _AdaptiveResult([("x",), ("y" * 900_000,), ("z" * 900_000,)])
    executor._engine = Mock(connect=Mock(return_value=_Connection(result)))

    query = Mock(sql="SELECT payload FROM dw.items", params={})
    batches = [batch async for batch in executor.execute(query)]

    assert result.partition_sizes[:2] == [1, 2]
    assert sum(len(batch.rows) for batch in batches) == 3


async def test_query_executor_caps_speculative_driver_growth() -> None:
    """未知下一行宽度时批量读取可增长，但预物化行数必须有硬上限。"""
    executor = MySQLQueryExecutor(
        "mysql+asyncmy://query:secret@localhost/dw",
        timeout_seconds=10,
        fetch_batch_rows=500,
        max_batch_bytes=1024 * 1024,
    )
    result = _AdaptiveResult([("x",)] * 20)
    executor._engine = Mock(connect=Mock(return_value=_Connection(result)))

    query = Mock(sql="SELECT payload FROM dw.items", params={})
    batches = [batch async for batch in executor.execute(query)]

    assert max(result.partition_sizes) == 4
    assert sum(len(batch.rows) for batch in batches) == 20


def test_query_engine_initializes_utc_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只读连接必须固定 UTC，避免 TIMESTAMP 查询随服务器时区漂移。"""
    engine = Mock()
    create_engine = Mock(return_value=engine)
    monkeypatch.setattr(mysql_adapter, "create_async_engine", create_engine)

    MySQLQueryExecutor(
        "mysql+asyncmy://query:secret@localhost/dw",
        timeout_seconds=10,
        fetch_batch_rows=500,
        max_batch_bytes=1024,
    )

    assert create_engine.call_args.kwargs["connect_args"] == {
        "init_command": "SET time_zone = '+00:00'"
    }


async def test_query_executor_probes_named_timezone_support() -> None:
    """IANA 时区只有在 DW 实例能够解析时才可用于分桶。"""
    executor = MySQLQueryExecutor(
        "mysql+asyncmy://query:secret@localhost/dw",
        timeout_seconds=10,
        fetch_batch_rows=500,
        max_batch_bytes=1024,
    )
    connection = _Connection(_Result())
    connection.scalar = AsyncMock(return_value=1)  # type: ignore[method-assign]
    executor._engine = Mock(connect=Mock(return_value=connection))

    await executor.ensure_timezone_supported("Asia/Shanghai")

    assert connection.scalar.await_count == 1
