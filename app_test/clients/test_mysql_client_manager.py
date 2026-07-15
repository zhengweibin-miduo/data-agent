"""MySQL 异步客户端生命周期检查。"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.clients.mysql_client_manager import MysqlClientManager


async def _test_mysql_client() -> None:
    client = MysqlClientManager.initialize()
    try:
        assert isinstance(client, AsyncEngine)
        assert MysqlClientManager.get_client() is client
        assert MysqlClientManager.initialize() is client
        assert client.url.drivername == "mysql+asyncmy"
        async with client.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
    finally:
        await MysqlClientManager.close()


def test_mysql_client() -> None:
    asyncio.run(_test_mysql_client())


if __name__ == "__main__":
    test_mysql_client()
