"""MySQL 异步客户端与 Session 生命周期检查。"""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from data_agent.infrastructure.mysql import MySQLDatabase
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)


class _RollbackSignal(Exception):
    """触发并验证事务回滚。"""


async def _test_database_configuration() -> None:
    """验证无需连接数据库的引擎与 Session 配置。"""
    await MySQLDatabase.close()

    try:
        MySQLDatabase.get_client()
    except RuntimeError as error:
        check_exception(
            "_test_database_configuration 捕获预期异常", error, RuntimeError
        )
        check_condition(
            "_test_database_configuration 检查点 1",
            "MySQLDatabase.initialize()" in str(error),
            expected="原断言条件成立",
        )
    else:
        fail_check(
            "_test_database_configuration",
            actual="未抛出预期异常",
            expected="未初始化时不应返回 MySQL 引擎",
        )

    try:
        async with MySQLDatabase.session():
            pass
    except RuntimeError as error:
        check_exception(
            "_test_database_configuration 捕获预期异常", error, RuntimeError
        )
        check_condition(
            "_test_database_configuration 检查点 2",
            "MySQLDatabase.initialize()" in str(error),
            expected="原断言条件成立",
        )
    else:
        fail_check(
            "_test_database_configuration",
            actual="未抛出预期异常",
            expected="未初始化时不应创建 MySQL Session",
        )

    client = MySQLDatabase.initialize()
    session_factory = MySQLDatabase._session_factory
    try:
        check_condition(
            "_test_database_configuration 检查点 3",
            isinstance(client, AsyncEngine),
            expected="原断言条件成立",
        )
        check_condition(
            "_test_database_configuration 检查点 4",
            MySQLDatabase.get_client() is client,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_database_configuration 检查点 5",
            MySQLDatabase.initialize() is client,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_database_configuration 检查点 6",
            MySQLDatabase._session_factory is session_factory,
            expected="原断言条件成立",
        )
        check_equal(
            "_test_database_configuration 检查点 7",
            client.url.drivername,
            "mysql+asyncmy",
        )
        check_condition(
            "_test_database_configuration 检查点 8",
            client.pool._pre_ping is True,
            expected="原断言条件成立",
        )
        check_equal(
            "_test_database_configuration 检查点 9",
            client.pool._recycle,
            3600,
        )

        barrier = asyncio.Barrier(2)

        async def capture_session() -> AsyncSession:
            async with MySQLDatabase.session() as session:
                await barrier.wait()
                return session

        first_session, second_session = await asyncio.gather(
            capture_session(),
            capture_session(),
        )
        check_condition(
            "_test_database_configuration 检查点 10",
            isinstance(first_session, AsyncSession),
            expected="原断言条件成立",
        )
        check_condition(
            "_test_database_configuration 检查点 11",
            isinstance(second_session, AsyncSession),
            expected="原断言条件成立",
        )
        check_condition(
            "_test_database_configuration 检查点 12",
            first_session is not second_session,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_database_configuration 检查点 13",
            first_session.bind is client,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_database_configuration 检查点 14",
            second_session.bind is client,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_database_configuration 检查点 15",
            first_session.sync_session.expire_on_commit is False,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_database_configuration 检查点 16",
            second_session.sync_session.expire_on_commit is False,
            expected="原断言条件成立",
        )
    finally:
        await MySQLDatabase.close()

    check_condition(
        "_test_database_configuration 检查点 17",
        MySQLDatabase._client is None,
        expected="原断言条件成立",
    )
    check_condition(
        "_test_database_configuration 检查点 18",
        MySQLDatabase._session_factory is None,
        expected="原断言条件成立",
    )

    new_client = MySQLDatabase.initialize()
    try:
        check_condition(
            "_test_database_configuration 检查点 19",
            new_client is not client,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_database_configuration 检查点 20",
            MySQLDatabase._session_factory is not session_factory,
            expected="原断言条件成立",
        )
    finally:
        await MySQLDatabase.close()


async def _test_session_transaction_lifecycle() -> None:
    """验证正常提交、异常回滚以及两条路径都关闭 Session。"""
    normal_session = AsyncMock(spec=AsyncSession)
    normal_session.__aenter__.return_value = normal_session
    normal_factory = Mock(return_value=normal_session)

    with patch.object(MySQLDatabase, "_session_factory", normal_factory):
        async with MySQLDatabase.session() as session:
            check_condition(
                "_test_session_transaction_lifecycle 检查点 1",
                session is normal_session,
                expected="原断言条件成立",
            )

    normal_session.commit.assert_awaited_once_with()
    normal_session.rollback.assert_not_awaited()
    normal_session.__aexit__.assert_awaited_once()

    failed_session = AsyncMock(spec=AsyncSession)
    failed_session.__aenter__.return_value = failed_session
    failed_factory = Mock(return_value=failed_session)
    signal = _RollbackSignal()

    try:
        with patch.object(MySQLDatabase, "_session_factory", failed_factory):
            async with MySQLDatabase.session():
                raise signal
    except _RollbackSignal as error:
        check_exception(
            "_test_session_transaction_lifecycle 捕获预期异常", error, _RollbackSignal
        )
        check_condition(
            "_test_session_transaction_lifecycle 检查点 2",
            error is signal,
            expected="原断言条件成立",
        )
    else:
        fail_check(
            "_test_session_transaction_lifecycle",
            actual="未抛出预期异常",
            expected="Session 上下文必须继续抛出业务异常",
        )

    failed_session.commit.assert_not_awaited()
    failed_session.rollback.assert_awaited_once_with()
    failed_session.__aexit__.assert_awaited_once()

    client = Mock(spec=AsyncEngine)
    replacement_client: AsyncEngine | None = None

    async def dispose_and_reinitialize() -> None:
        nonlocal replacement_client
        check_condition(
            "dispose_and_reinitialize 检查点 1",
            MySQLDatabase._client is None,
            expected="原断言条件成立",
        )
        check_condition(
            "dispose_and_reinitialize 检查点 2",
            MySQLDatabase._session_factory is None,
            expected="原断言条件成立",
        )
        replacement_client = MySQLDatabase.initialize()

    client.dispose = AsyncMock(side_effect=dispose_and_reinitialize)
    session_factory = Mock()
    with (
        patch.object(MySQLDatabase, "_client", client),
        patch.object(MySQLDatabase, "_session_factory", session_factory),
    ):
        await MySQLDatabase.close()
        client.dispose.assert_awaited_once_with()
        check_condition(
            "_test_session_transaction_lifecycle 检查点 3",
            MySQLDatabase._client is replacement_client,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_session_transaction_lifecycle 检查点 4",
            replacement_client is not client,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_session_transaction_lifecycle 检查点 5",
            MySQLDatabase._session_factory is not None,
            expected="原断言条件成立",
        )

        await MySQLDatabase.close()
        check_condition(
            "_test_session_transaction_lifecycle 检查点 6",
            MySQLDatabase._client is None,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_session_transaction_lifecycle 检查点 7",
            MySQLDatabase._session_factory is None,
            expected="原断言条件成立",
        )


async def _test_mysql_client_integration() -> None:
    """连接真实 MySQL，验证引擎、提交和回滚。"""
    client = MySQLDatabase.initialize()
    try:
        async with client.connect() as connection:
            check_equal(
                "_test_mysql_client_integration 检查点 1",
                await connection.scalar(text("SELECT 1")),
                1,
            )

        async with client.begin() as connection:
            await connection.execute(
                text("DROP TABLE IF EXISTS session_transaction_test")
            )

        try:
            async with MySQLDatabase.session() as session:
                check_equal(
                    "_test_mysql_client_integration 检查点 2",
                    await session.scalar(text("SELECT 1")),
                    1,
                )
                await session.execute(
                    text(
                        "CREATE TABLE session_transaction_test "
                        "(value INTEGER NOT NULL) ENGINE=InnoDB"
                    )
                )
                await session.execute(
                    text("INSERT INTO session_transaction_test (value) VALUES (1)")
                )

            async with MySQLDatabase.session() as session:
                check_equal(
                    "_test_mysql_client_integration 检查点 3",
                    await session.scalar(
                        text("SELECT COUNT(*) FROM session_transaction_test")
                    ),
                    1,
                )

            try:
                async with MySQLDatabase.session() as session:
                    await session.execute(
                        text("INSERT INTO session_transaction_test (value) VALUES (2)")
                    )
                    raise _RollbackSignal()
            except _RollbackSignal:
                pass

            async with MySQLDatabase.session() as session:
                check_equal(
                    "_test_mysql_client_integration 检查点 4",
                    await session.scalar(
                        text("SELECT COUNT(*) FROM session_transaction_test")
                    ),
                    1,
                )
        finally:
            async with client.begin() as connection:
                await connection.execute(
                    text("DROP TABLE IF EXISTS session_transaction_test")
                )
    finally:
        await MySQLDatabase.close()


@pytest.mark.integration
async def test_mysql_database_configuration() -> None:
    """运行不依赖 MySQL 服务的数据库生命周期测试。"""
    await _test_database_configuration()
    await _test_session_transaction_lifecycle()


@pytest.mark.integration
async def test_mysql_client() -> None:
    """运行真实 MySQL 集成测试。"""
    await _test_mysql_client_integration()
