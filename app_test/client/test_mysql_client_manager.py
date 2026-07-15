"""MySQL 异步客户端与 Session 生命周期检查。"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.client.mysql_client_manager import MysqlClientManager


class _RollbackSignal(Exception):
    """触发并验证事务回滚。"""


async def _test_manager_configuration() -> None:
    """验证无需连接数据库的引擎与 Session 配置。"""
    await MysqlClientManager.close()

    try:
        MysqlClientManager.get_client()
    except RuntimeError as error:
        assert "MysqlClientManager.initialize()" in str(error)
    else:
        raise AssertionError("未初始化时不应返回 MySQL 引擎")

    try:
        async with MysqlClientManager.session():
            pass
    except RuntimeError as error:
        assert "MysqlClientManager.initialize()" in str(error)
    else:
        raise AssertionError("未初始化时不应创建 MySQL Session")

    client = MysqlClientManager.initialize()
    session_factory = MysqlClientManager._session_factory
    try:
        assert isinstance(client, AsyncEngine)
        assert MysqlClientManager.get_client() is client
        assert MysqlClientManager.initialize() is client
        assert MysqlClientManager._session_factory is session_factory
        assert client.url.drivername == "mysql+asyncmy"
        assert client.pool._pre_ping is True
        assert client.pool._recycle == 3600

        barrier = asyncio.Barrier(2)

        async def capture_session() -> AsyncSession:
            async with MysqlClientManager.session() as session:
                await barrier.wait()
                return session

        first_session, second_session = await asyncio.gather(
            capture_session(),
            capture_session(),
        )
        assert isinstance(first_session, AsyncSession)
        assert isinstance(second_session, AsyncSession)
        assert first_session is not second_session
        assert first_session.bind is client
        assert second_session.bind is client
        assert first_session.sync_session.expire_on_commit is False
        assert second_session.sync_session.expire_on_commit is False
    finally:
        await MysqlClientManager.close()

    assert MysqlClientManager._client is None
    assert MysqlClientManager._session_factory is None

    new_client = MysqlClientManager.initialize()
    try:
        assert new_client is not client
        assert MysqlClientManager._session_factory is not session_factory
    finally:
        await MysqlClientManager.close()


async def _test_session_transaction_lifecycle() -> None:
    """验证正常提交、异常回滚以及两条路径都关闭 Session。"""
    normal_session = AsyncMock(spec=AsyncSession)
    normal_session.__aenter__.return_value = normal_session
    normal_factory = Mock(return_value=normal_session)

    with patch.object(MysqlClientManager, "_session_factory", normal_factory):
        async with MysqlClientManager.session() as session:
            assert session is normal_session

    normal_session.commit.assert_awaited_once_with()
    normal_session.rollback.assert_not_awaited()
    normal_session.__aexit__.assert_awaited_once()

    failed_session = AsyncMock(spec=AsyncSession)
    failed_session.__aenter__.return_value = failed_session
    failed_factory = Mock(return_value=failed_session)
    signal = _RollbackSignal()

    try:
        with patch.object(MysqlClientManager, "_session_factory", failed_factory):
            async with MysqlClientManager.session():
                raise signal
    except _RollbackSignal as error:
        assert error is signal
    else:
        raise AssertionError("Session 上下文必须继续抛出业务异常")

    failed_session.commit.assert_not_awaited()
    failed_session.rollback.assert_awaited_once_with()
    failed_session.__aexit__.assert_awaited_once()

    client = Mock(spec=AsyncEngine)
    replacement_client: AsyncEngine | None = None

    async def dispose_and_reinitialize() -> None:
        nonlocal replacement_client
        assert MysqlClientManager._client is None
        assert MysqlClientManager._session_factory is None
        replacement_client = MysqlClientManager.initialize()

    client.dispose = AsyncMock(side_effect=dispose_and_reinitialize)
    session_factory = Mock()
    with (
        patch.object(MysqlClientManager, "_client", client),
        patch.object(MysqlClientManager, "_session_factory", session_factory),
    ):
        await MysqlClientManager.close()
        client.dispose.assert_awaited_once_with()
        assert MysqlClientManager._client is replacement_client
        assert replacement_client is not client
        assert MysqlClientManager._session_factory is not None

        await MysqlClientManager.close()
        assert MysqlClientManager._client is None
        assert MysqlClientManager._session_factory is None


async def _test_mysql_client_integration() -> None:
    """连接真实 MySQL，验证引擎、提交和回滚。"""
    client = MysqlClientManager.initialize()
    try:
        async with client.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1

        async with client.begin() as connection:
            await connection.execute(
                text("DROP TABLE IF EXISTS session_transaction_test")
            )

        try:
            async with MysqlClientManager.session() as session:
                assert await session.scalar(text("SELECT 1")) == 1
                await session.execute(
                    text(
                        "CREATE TABLE session_transaction_test "
                        "(value INTEGER NOT NULL) ENGINE=InnoDB"
                    )
                )
                await session.execute(
                    text("INSERT INTO session_transaction_test (value) VALUES (1)")
                )

            async with MysqlClientManager.session() as session:
                assert (
                    await session.scalar(
                        text("SELECT COUNT(*) FROM session_transaction_test")
                    )
                    == 1
                )

            try:
                async with MysqlClientManager.session() as session:
                    await session.execute(
                        text("INSERT INTO session_transaction_test (value) VALUES (2)")
                    )
                    raise _RollbackSignal()
            except _RollbackSignal:
                pass

            async with MysqlClientManager.session() as session:
                assert (
                    await session.scalar(
                        text("SELECT COUNT(*) FROM session_transaction_test")
                    )
                    == 1
                )
        finally:
            async with client.begin() as connection:
                await connection.execute(
                    text("DROP TABLE IF EXISTS session_transaction_test")
                )
    finally:
        await MysqlClientManager.close()


def test_mysql_client_manager() -> None:
    """运行不依赖 MySQL 服务的管理器测试。"""
    asyncio.run(_test_manager_configuration())
    asyncio.run(_test_session_transaction_lifecycle())


def test_mysql_client() -> None:
    """运行真实 MySQL 集成测试。"""
    asyncio.run(_test_mysql_client_integration())


if __name__ == "__main__":
    test_mysql_client_manager()
    test_mysql_client()
