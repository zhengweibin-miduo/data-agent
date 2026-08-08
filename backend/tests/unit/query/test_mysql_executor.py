"""只读 MySQL 执行器的连接契约测试。"""

from unittest.mock import Mock

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


class _Connection:
    def __init__(self, result: _Result) -> None:
        self.result = result

    async def start(self) -> None:
        return None

    async def exec_driver_sql(self, _sql: str) -> None:
        return None

    async def stream(self, _statement: object, _params: object) -> _Result:
        return self.result

    async def rollback(self) -> None:
        return None

    def in_transaction(self) -> bool:
        return False

    async def close(self) -> None:
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
