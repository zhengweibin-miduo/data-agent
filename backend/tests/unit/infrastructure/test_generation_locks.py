"""专用 generation lock manager 容量、错误翻译与清理测试。"""

import asyncio
from typing import Any, cast

import pytest
from pytest import MonkeyPatch
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

from infrastructure import generation_locks as module
from infrastructure.generation_locks import GenerationLockManager
from infrastructure.mysql import (
    AdvisoryLockReleaseError,
    AdvisoryLockUnavailableError,
)


class _Connection:
    """按脚本返回 scalar 结果并记录 SQL/参数的 owner 连接替身。"""

    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.invalidated = False

    async def scalar(self, statement: object, parameters: dict[str, object]):
        self.calls.append((str(statement), parameters))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def invalidate(self, _error: BaseException) -> None:
        self.invalidated = True

    async def close(self) -> None:
        self.invalidated = True


class _ConnectionContext:
    """模拟 SQLAlchemy connect() 返回的异步上下文。"""

    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _Connection:
        return self._connection

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Engine:
    """记录关闭的最小引擎替身。"""

    def __init__(
        self,
        connection: _Connection | None = None,
        *,
        checkout_error: BaseException | None = None,
    ) -> None:
        self.closed = False
        self.connect_count = 0
        self.connection = connection
        self.checkout_error = checkout_error

    def connect(self) -> _ConnectionContext:
        self.connect_count += 1
        if self.checkout_error is not None:
            raise self.checkout_error
        assert self.connection is not None
        return _ConnectionContext(self.connection)

    async def dispose(self) -> None:
        """记录专用池已释放。"""
        self.closed = True


async def test_manager_uses_a_bounded_dedicated_pool_and_closes_it(
    monkeypatch: MonkeyPatch,
) -> None:
    """Owner 引擎必须禁用 overflow 并使用显式 checkout timeout。"""
    captured: dict[str, Any] = {}
    engine = _Engine()

    def create(url: str, **kwargs: Any) -> AsyncEngine:
        captured.update(url=url, **kwargs)
        return cast(AsyncEngine, engine)

    monkeypatch.setattr(module, "create_async_engine", create)
    manager = GenerationLockManager(
        "mysql+asyncmy://user:pass@localhost/meta",
        pool_size=16,
        pool_timeout_seconds=1,
    )

    await manager.initialize()
    await manager.initialize()
    await manager.close()

    assert captured["pool_size"] == 16
    assert captured["max_overflow"] == 0
    assert captured["pool_timeout"] == 1
    assert captured["connect_args"] == {"init_command": "SET time_zone = '+00:00'"}
    assert engine.closed is True


async def _manager(monkeypatch: MonkeyPatch, engine: _Engine) -> GenerationLockManager:
    """经公共 initialize seam 装配指定专用引擎。"""
    monkeypatch.setattr(
        module,
        "create_async_engine",
        lambda *_args, **_kwargs: cast(AsyncEngine, engine),
    )
    manager = GenerationLockManager("mysql+asyncmy://user:pass@localhost/meta")
    await manager.initialize()
    return manager


async def test_read_acquires_sorted_targets_atomically_and_releases(
    monkeypatch: MonkeyPatch,
) -> None:
    """READ set 使用单次排序调用，并在正常退出时释放 namespace。"""
    connection = _Connection([1, 1])
    manager = await _manager(monkeypatch, _Engine(connection))

    async with manager.read(["table:b", "table:a"], 3):
        pass

    acquire_sql, parameters = connection.calls[0]
    assert "service_get_read_locks" in acquire_sql
    assert parameters["lock_0"] == "table:a"
    assert parameters["lock_1"] == "table:b"
    assert "service_release_locks" in connection.calls[1][0]


async def test_expandable_write_reuses_one_owner_connection(
    monkeypatch: MonkeyPatch,
) -> None:
    """发布者扩展目标锁时不得为嵌套锁再次 checkout。"""
    connection = _Connection([1, 1, 1])
    engine = _Engine(connection)
    manager = await _manager(monkeypatch, engine)

    async with manager.expandable_write(["publisher:source"], 3) as owner:
        await owner.acquire(["table:a", "table:b"], 3)

    assert engine.connect_count == 1
    assert "service_get_write_locks" in connection.calls[0][0]
    assert "service_get_write_locks" in connection.calls[1][0]
    assert "service_release_locks" in connection.calls[2][0]


async def test_checkout_exhaustion_has_stable_unavailable_error(
    monkeypatch: MonkeyPatch,
) -> None:
    """专用池 checkout timeout 不泄漏 SQLAlchemy 异常。"""
    manager = await _manager(
        monkeypatch,
        _Engine(checkout_error=SQLAlchemyTimeoutError("pool full")),
    )

    with pytest.raises(AdvisoryLockUnavailableError, match="owner 池已满"):
        async with manager.write(["table:a"], 0):
            pytest.fail("checkout 失败不得进入临界区")


async def test_release_failure_invalidates_owner_connection(
    monkeypatch: MonkeyPatch,
) -> None:
    """Namespace release 失败必须摘除 owner 连接并暴露稳定错误。"""
    connection = _Connection([1, 0])
    manager = await _manager(monkeypatch, _Engine(connection))

    with pytest.raises(AdvisoryLockReleaseError):
        async with manager.write(["table:a"], 0):
            pass

    assert connection.invalidated is True


async def test_release_failure_preserves_active_business_error(
    monkeypatch: MonkeyPatch,
) -> None:
    """清理失败必须失效连接，但不得覆盖临界区内的业务异常。"""
    connection = _Connection([1, 0])
    manager = await _manager(monkeypatch, _Engine(connection))

    with pytest.raises(ValueError, match="business failed"):
        async with manager.read(["table:a"], 0):
            raise ValueError("business failed")

    assert connection.invalidated is True


async def test_cancellation_releases_before_propagating(
    monkeypatch: MonkeyPatch,
) -> None:
    """调用方取消仍执行 namespace release，并保留 CancelledError。"""
    connection = _Connection([1, 1])
    manager = await _manager(monkeypatch, _Engine(connection))

    with pytest.raises(asyncio.CancelledError):
        async with manager.read(["table:a"], 0):
            raise asyncio.CancelledError

    assert "service_release_locks" in connection.calls[-1][0]
