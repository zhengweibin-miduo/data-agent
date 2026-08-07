"""MySQL advisory lock 生命周期单元检查。"""

import asyncio
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from data_sync.locks import generation_lock_name
from infrastructure.mysql import (
    AdvisoryLockReleaseError,
    AdvisoryLockUnavailableError,
    LockingServiceUnavailableError,
    MySQLDatabase,
)
from tests.helpers.checks import check_condition, check_equal, check_exception


class _FakeConnection:
    """记录命名锁 SQL、返回值与连接失效动作。"""

    def __init__(
        self,
        *,
        get_results: dict[str, object] | None = None,
        release_results: dict[str, object] | None = None,
    ) -> None:
        self.actions: list[tuple[str, str]] = []
        self.get_results = get_results or {}
        self.release_results = release_results or {}
        self.invalidated_with: BaseException | None = None
        self.closed = False

    async def scalar(
        self,
        statement: object,
        parameters: dict[str, object],
    ) -> object:
        """按 SQL 类型返回配置结果并记录锁名。"""
        name = str(parameters["lock_name"])
        action = "get" if "GET_LOCK" in str(statement) else "release"
        self.actions.append((action, name))
        results = self.get_results if action == "get" else self.release_results
        result = results.get(name, 1)
        if isinstance(result, BaseException):
            raise result
        return result

    async def invalidate(self, error: BaseException) -> None:
        """记录连接因释放失败被禁止回池。"""
        self.invalidated_with = error

    async def close(self) -> None:
        """记录 invalidate 自身失败时的关闭兜底。"""
        self.closed = True


class _FakeConnectionContext(AbstractAsyncContextManager[_FakeConnection]):
    """为专用 owner 连接提供异步上下文。"""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _FakeConnection:
        """返回专用 owner 连接。"""
        return self._connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """不吞掉业务或清理异常。"""
        return None


class _FakeEngine:
    """仅实现命名锁上下文需要的 connect 边界。"""

    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    def connect(self) -> _FakeConnectionContext:
        """返回可跟踪的专用连接上下文。"""
        return _FakeConnectionContext(self._connection)


class _FakeServiceConnection(_FakeConnection):
    """记录 Locking Service 单次多锁调用与 namespace 释放。"""

    def __init__(self, results: list[object] | None = None) -> None:
        super().__init__()
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._results = iter(results or [1, 1])

    async def scalar(
        self,
        statement: object,
        parameters: dict[str, object],
    ) -> object:
        """记录完整 SQL 边界并按顺序返回结果或异常。"""
        self.calls.append((str(statement), dict(parameters)))
        result = next(self._results)
        if isinstance(result, BaseException):
            raise result
        return result


async def test_advisory_locks_order_deduplicate_and_release_on_error() -> None:
    """多锁必须去重排序，并在业务异常时逆序完整释放。"""
    connection = _FakeConnection()
    engine = _FakeEngine(connection)
    signal = LookupError("business failure")

    with (
        patch.object(MySQLDatabase, "get_client", return_value=engine),
        pytest.raises(LookupError) as captured,
    ):
        async with MySQLDatabase.advisory_locks(
            ["锁-b", "lock-a", "锁-b"],
            timeout_seconds=1,
        ):
            raise signal

    check_condition("业务异常保持原实例", captured.value is signal)
    check_equal(
        "命名锁按 UTF-8 顺序获取并逆序释放",
        connection.actions,
        [
            ("get", "lock-a"),
            ("get", "锁-b"),
            ("release", "锁-b"),
            ("release", "lock-a"),
        ],
    )


async def test_advisory_locks_release_partial_acquisition() -> None:
    """后续锁超时时必须释放此前已经取得的锁。"""
    connection = _FakeConnection(get_results={"lock-b": 0})
    engine = _FakeEngine(connection)

    with (
        patch.object(MySQLDatabase, "get_client", return_value=engine),
        pytest.raises(AdvisoryLockUnavailableError) as captured,
    ):
        async with MySQLDatabase.advisory_locks(
            ["lock-b", "lock-a"],
            timeout_seconds=0,
        ):
            pytest.fail("部分获取失败后不得进入业务上下文")

    check_exception(
        "部分获取返回可重试锁异常",
        captured.value,
        AdvisoryLockUnavailableError,
    )
    check_equal(
        "部分获取失败释放已持有锁",
        connection.actions,
        [("get", "lock-a"), ("get", "lock-b"), ("release", "lock-a")],
    )


async def test_advisory_locks_invalidate_connection_on_release_failure() -> None:
    """释放失败必须使 owner 连接失效，禁止锁随连接回到池中。"""
    failure = ConnectionError("release failed")
    connection = _FakeConnection(release_results={"lock-a": failure})
    engine = _FakeEngine(connection)

    with (
        patch.object(MySQLDatabase, "get_client", return_value=engine),
        pytest.raises(AdvisoryLockReleaseError) as captured,
    ):
        async with MySQLDatabase.advisory_locks(
            ["lock-a"],
            timeout_seconds=1,
        ):
            pass

    check_exception("释放失败投影为明确异常", captured.value, AdvisoryLockReleaseError)
    check_condition("释放失败使连接失效", connection.invalidated_with is failure)


async def test_release_failure_does_not_replace_active_business_error() -> None:
    """业务异常与释放失败并发时保留业务异常，同时使 owner 连接失效。"""
    release_failure = ConnectionError("release failed")
    business_error = ValueError("business failed")
    connection = _FakeConnection(release_results={"lock-a": release_failure})
    engine = _FakeEngine(connection)

    with (
        patch.object(MySQLDatabase, "get_client", return_value=engine),
        pytest.raises(ValueError) as captured,
    ):
        async with MySQLDatabase.advisory_locks(
            ["lock-a"],
            timeout_seconds=1,
        ):
            raise business_error

    check_condition("清理失败不覆盖业务异常", captured.value is business_error)
    check_condition(
        "业务异常路径仍使连接失效",
        connection.invalidated_with is release_failure,
    )


async def test_cancellation_releases_lock_and_keeps_cancellation() -> None:
    """任务取消仍须释放命名锁，并把取消原样传播给上层。"""
    connection = _FakeConnection()
    engine = _FakeEngine(connection)

    with (
        patch.object(MySQLDatabase, "get_client", return_value=engine),
        pytest.raises(asyncio.CancelledError),
    ):
        async with MySQLDatabase.advisory_locks(
            ["lock-a"],
            timeout_seconds=1,
        ):
            raise asyncio.CancelledError

    check_equal(
        "取消路径释放已取得锁",
        connection.actions,
        [("get", "lock-a"), ("release", "lock-a")],
    )
    check_equal("正常释放无需使连接失效", connection.invalidated_with, None)


def test_generation_lock_name_is_stable_bounded_and_target_scoped() -> None:
    """Generation 锁名必须稳定、限长且按二进制 target 身份隔离。"""
    first = generation_lock_name("dw", "订单" * 40)
    repeated = generation_lock_name("dw", "订单" * 40)
    other_target = generation_lock_name("dw", "订单" * 39 + "单据")
    other_database = generation_lock_name("dw_other", "订单" * 40)

    check_equal("同一 target 锁名稳定", first, repeated)
    check_condition("锁名不超过 MySQL 64 字节", len(first.encode("utf-8")) <= 64)
    check_condition("不同 target 不共享锁", first != other_target)
    check_condition("不同 DW 数据库不共享锁", first != other_database)


@pytest.mark.parametrize("timeout_seconds", [-1, float("inf"), float("nan")])
async def test_advisory_locks_reject_invalid_timeout(timeout_seconds: float) -> None:
    """命名锁等待预算必须是有限非负秒数。"""
    with pytest.raises(ValueError):
        async with MySQLDatabase.advisory_locks(
            ["lock-a"],
            timeout_seconds=timeout_seconds,
        ):
            pass


async def test_shared_service_locks_acquire_all_names_atomically() -> None:
    """共享 generation 锁必须排序去重后通过一次 SQL 原子获取。"""
    connection = _FakeServiceConnection()
    engine = _FakeEngine(connection)

    with patch.object(MySQLDatabase, "get_client", return_value=engine):
        async with MySQLDatabase.shared_service_locks(
            ["锁-b", "lock-a", "锁-b"],
            timeout_seconds=2,
        ):
            pass

    acquire_sql, acquire_parameters = connection.calls[0]
    release_sql, release_parameters = connection.calls[1]
    assert "service_get_read_locks" in acquire_sql
    assert acquire_parameters == {
        "namespace": "data-agent-generation-v1",
        "lock_0": "lock-a",
        "lock_1": "锁-b",
        "timeout_seconds": 2,
    }
    assert "service_release_locks" in release_sql
    assert release_parameters == {"namespace": "data-agent-generation-v1"}


async def test_exclusive_service_locks_use_write_function() -> None:
    """独占 generation 锁必须使用 Locking Service WRITE 接口。"""
    connection = _FakeServiceConnection()
    engine = _FakeEngine(connection)

    with patch.object(MySQLDatabase, "get_client", return_value=engine):
        async with MySQLDatabase.exclusive_service_locks(
            ["lock-a"],
            timeout_seconds=1,
        ):
            pass

    assert "service_get_write_locks" in connection.calls[0][0]


@pytest.mark.parametrize("error_number", [3132, 3133])
async def test_service_lock_contention_has_stable_error(error_number: int) -> None:
    """Locking Service deadlock 与 timeout 都映射为既有资源忙异常。"""
    failure = OperationalError(
        "SELECT service_get_read_locks(...) ",
        {},
        Exception(error_number, "locking service busy"),
    )
    connection = _FakeServiceConnection([failure, 1])
    engine = _FakeEngine(connection)

    with (
        patch.object(MySQLDatabase, "get_client", return_value=engine),
        pytest.raises(AdvisoryLockUnavailableError) as captured,
    ):
        async with MySQLDatabase.shared_service_locks(
            ["lock-a"],
            timeout_seconds=0,
        ):
            pytest.fail("锁竞争时不得进入 generation 临界区")

    assert captured.value.__cause__ is failure
    assert "service_release_locks" in connection.calls[1][0]


async def test_service_lock_release_failure_preserves_business_error() -> None:
    """Generation 清理失败必须失效 owner 连接且不覆盖活动业务异常。"""
    release_failure = ConnectionError("release failed")
    business_error = ValueError("business failed")
    connection = _FakeServiceConnection([1, release_failure])
    engine = _FakeEngine(connection)

    with (
        patch.object(MySQLDatabase, "get_client", return_value=engine),
        pytest.raises(ValueError) as captured,
    ):
        async with MySQLDatabase.shared_service_locks(
            ["lock-a"],
            timeout_seconds=1,
        ):
            raise business_error

    assert captured.value is business_error
    assert connection.invalidated_with is release_failure


async def test_service_lock_cancellation_releases_and_propagates() -> None:
    """Generation 临界区取消必须释放 namespace 并原样传播取消。"""
    connection = _FakeServiceConnection([1, 1])
    engine = _FakeEngine(connection)

    with (
        patch.object(MySQLDatabase, "get_client", return_value=engine),
        pytest.raises(asyncio.CancelledError),
    ):
        async with MySQLDatabase.exclusive_service_locks(
            ["lock-a"],
            timeout_seconds=1,
        ):
            raise asyncio.CancelledError

    assert "service_release_locks" in connection.calls[1][0]
    assert connection.invalidated_with is None


async def test_locking_service_probe_rejects_missing_functions() -> None:
    """启动 probe 必须把缺失 SQL functions 投影为明确能力错误。"""
    failure = OperationalError(
        "SELECT service_release_locks(...) ",
        {},
        Exception(1305, "FUNCTION service_release_locks does not exist"),
    )
    connection = _FakeServiceConnection([failure, 1])
    engine = _FakeEngine(connection)

    with (
        patch.object(MySQLDatabase, "get_client", return_value=engine),
        pytest.raises(LockingServiceUnavailableError) as captured,
    ):
        await MySQLDatabase.check_locking_service()

    assert captured.value.__cause__ is failure


async def test_locking_service_probe_rejects_partial_function_install() -> None:
    """READ 已安装但 WRITE 缺失时仍必须在启动阶段失败关闭。"""
    failure = OperationalError(
        "SELECT service_get_write_locks(...) ",
        {},
        Exception(1305, "FUNCTION service_get_write_locks does not exist"),
    )
    connection = _FakeServiceConnection([1, failure, 1])
    engine = _FakeEngine(connection)

    with (
        patch.object(MySQLDatabase, "get_client", return_value=engine),
        pytest.raises(LockingServiceUnavailableError) as captured,
    ):
        await MySQLDatabase.check_locking_service()

    assert captured.value.__cause__ is failure


async def test_locking_service_probe_rejects_missing_release_function() -> None:
    """READ/WRITE 已安装但 release 缺失时必须失效 probe owner 连接。"""
    failure = OperationalError(
        "SELECT service_release_locks(...) ",
        {},
        Exception(1305, "FUNCTION service_release_locks does not exist"),
    )
    connection = _FakeServiceConnection([1, 1, failure])
    engine = _FakeEngine(connection)

    with (
        patch.object(MySQLDatabase, "get_client", return_value=engine),
        pytest.raises(LockingServiceUnavailableError) as captured,
    ):
        await MySQLDatabase.check_locking_service()

    assert captured.value.__cause__ is failure
    assert connection.invalidated_with is failure


async def test_locking_service_probe_uses_isolated_read_write_locks() -> None:
    """启动 probe 实际覆盖三个函数且不触碰 generation 业务 namespace。"""
    connection = _FakeServiceConnection([1, 1, 1])
    engine = _FakeEngine(connection)

    with patch.object(MySQLDatabase, "get_client", return_value=engine):
        await MySQLDatabase.check_locking_service()

    assert len(connection.calls) == 3
    assert "service_get_read_locks" in connection.calls[0][0]
    assert "service_get_write_locks" in connection.calls[1][0]
    assert "service_release_locks" in connection.calls[2][0]
    assert {
        parameters["namespace"] for _, parameters in connection.calls
    } == {"data-agent-capability-v1"}


async def test_locking_service_probe_uses_unique_names_per_owner() -> None:
    """并发进程的启动 probe 不得因固定 WRITE 名称互相误判能力缺失。"""
    first = _FakeServiceConnection([1, 1, 1])
    second = _FakeServiceConnection([1, 1, 1])

    with patch.object(
        MySQLDatabase,
        "get_client",
        side_effect=[_FakeEngine(first), _FakeEngine(second)],
    ):
        await MySQLDatabase.check_locking_service()
        await MySQLDatabase.check_locking_service()

    first_names = {
        str(parameters["lock_name"])
        for statement, parameters in first.calls
        if "service_get_" in statement
    }
    second_names = {
        str(parameters["lock_name"])
        for statement, parameters in second.calls
        if "service_get_" in statement
    }
    assert len(first_names) == 2
    assert len(second_names) == 2
    assert first_names.isdisjoint(second_names)


@pytest.mark.parametrize("timeout_seconds", [-1, 0.5, True])
async def test_service_locks_reject_non_integer_timeout(
    timeout_seconds: object,
) -> None:
    """Locking Service 等待预算必须是非负整数秒。"""
    with pytest.raises(ValueError):
        async with MySQLDatabase.shared_service_locks(
            ["lock-a"],
            timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
        ):
            pass
