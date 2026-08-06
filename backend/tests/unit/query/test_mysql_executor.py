"""只读 MySQL 执行器的连接契约测试。"""

from unittest.mock import Mock

import pytest

import query.adapters.mysql as mysql_adapter
from query.adapters.mysql import MySQLQueryExecutor


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
