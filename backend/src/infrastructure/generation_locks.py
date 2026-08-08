"""MySQL Locking Service generation owner 的专用有界连接池。"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Iterable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.sql.elements import TextClause

from infrastructure.mysql import (
    AdvisoryLockReleaseError,
    AdvisoryLockUnavailableError,
    LockingServiceUnavailableError,
    _invalidate_owner_connection,
    _mysql_error_number,
    _normalize_advisory_lock_names,
)

_GENERATION_LOCK_NAMESPACE = "data-agent-generation-v1"
_PROBE_NAMESPACE = "data-agent-capability-v1"
_CONTENTION_ERRORS = frozenset({3132, 3133})


class ExpandableWriteOwner:
    """在同一 owner connection 上逐步扩展 WRITE 锁集合。"""

    def __init__(self, connection: AsyncConnection) -> None:
        """绑定本次发布独占的 owner connection。"""
        self._connection = connection
        self._held: set[str] = set()

    async def acquire(self, names: Iterable[str], timeout_seconds: int) -> None:
        """原子取得尚未持有的名称，避免嵌套 checkout owner 池。"""
        ordered = tuple(
            name
            for name in _normalize_advisory_lock_names(names)
            if name not in self._held
        )
        if not ordered:
            return
        statement, parameters = _lock_statement(
            ordered,
            function_name="service_get_write_locks",
            timeout_seconds=timeout_seconds,
        )
        try:
            acquired = await self._connection.scalar(statement, parameters)
        except DBAPIError as error:
            if _mysql_error_number(error) in _CONTENTION_ERRORS:
                raise AdvisoryLockUnavailableError(
                    "MySQL generation lock 未在等待预算内取得"
                ) from error
            raise
        if not acquired:
            raise AdvisoryLockUnavailableError(
                "MySQL generation lock 未在等待预算内取得"
            )
        self._held.update(ordered)


def _lock_statement(
    ordered: tuple[str, ...], *, function_name: str, timeout_seconds: int
) -> tuple[TextClause, dict[str, object]]:
    """构造一次 Locking Service 多名称调用。"""
    if (
        not isinstance(timeout_seconds, int)
        or isinstance(timeout_seconds, bool)
        or timeout_seconds < 0
    ):
        raise ValueError("MySQL Locking Service timeout 必须是非负整数秒")
    parameters: dict[str, object] = {
        "namespace": _GENERATION_LOCK_NAMESPACE,
        "timeout_seconds": timeout_seconds,
    }
    arguments: list[str] = []
    for index, name in enumerate(ordered):
        key = f"lock_{index}"
        parameters[key] = name
        arguments.append(f":{key}")
    return (
        text(
            f"SELECT {function_name}(:namespace, "
            f"{', '.join(arguments)}, :timeout_seconds)"
        ),
        parameters,
    )


class GenerationLockManager:
    """为长时间 generation READ/WRITE owner 提供独立容量与生命周期。"""

    def __init__(
        self,
        url: str,
        *,
        pool_size: int = 16,
        pool_timeout_seconds: float = 1,
    ) -> None:
        """绑定连接地址与显式池容量，延迟创建引擎。"""
        self._url = url
        self._pool_size = pool_size
        self._pool_timeout_seconds = pool_timeout_seconds
        self._engine: AsyncEngine | None = None

    async def initialize(self) -> None:
        """幂等创建不借用业务 Session 池的 owner 引擎。"""
        if self._engine is None:
            self._engine = create_async_engine(
                self._url,
                pool_pre_ping=True,
                pool_recycle=3600,
                pool_size=self._pool_size,
                max_overflow=0,
                pool_timeout=self._pool_timeout_seconds,
                connect_args={"init_command": "SET time_zone = '+00:00'"},
            )

    def _client(self) -> AsyncEngine:
        """返回已初始化的专用引擎。"""
        if self._engine is None:
            raise RuntimeError("GenerationLockManager 尚未初始化")
        return self._engine

    def read(
        self, names: Iterable[str], timeout_seconds: int
    ) -> AbstractAsyncContextManager[None]:
        """原子取得排序后的共享 READ 锁集。"""
        return self._locks(
            names,
            function_name="service_get_read_locks",
            timeout_seconds=timeout_seconds,
        )

    def write(
        self, names: Iterable[str], timeout_seconds: int
    ) -> AbstractAsyncContextManager[None]:
        """原子取得排序后的独占 WRITE 锁集。"""
        return self._locks(
            names,
            function_name="service_get_write_locks",
            timeout_seconds=timeout_seconds,
        )

    def expandable_write(
        self, names: Iterable[str], timeout_seconds: int
    ) -> AbstractAsyncContextManager[ExpandableWriteOwner]:
        """用一个 owner connection 持有初始锁并允许后续扩展。"""
        return self._expandable_write(names, timeout_seconds)

    @asynccontextmanager
    async def _expandable_write(
        self, names: Iterable[str], timeout_seconds: int
    ) -> AsyncIterator[ExpandableWriteOwner]:
        owner: ExpandableWriteOwner | None = None
        try:
            async with self._client().connect() as connection:
                owner = ExpandableWriteOwner(connection)
                try:
                    await owner.acquire(names, timeout_seconds)
                    yield owner
                finally:
                    active_error = sys.exc_info()[1]
                    release_error = await self._release(connection)
                    if release_error is not None:
                        await _invalidate_owner_connection(connection, release_error)
                        if active_error is None:
                            raise AdvisoryLockReleaseError(
                                "MySQL generation lock 释放失败，owner 连接已失效"
                            ) from release_error
        except SQLAlchemyTimeoutError as error:
            raise AdvisoryLockUnavailableError(
                "MySQL generation lock owner 池已满"
            ) from error

    @asynccontextmanager
    async def _locks(
        self,
        names: Iterable[str],
        *,
        function_name: str,
        timeout_seconds: int,
    ) -> AsyncIterator[None]:
        """用一次 Locking Service 调用原子持有多目标。"""
        ordered = _normalize_advisory_lock_names(names)
        statement, parameters = _lock_statement(
            ordered,
            function_name=function_name,
            timeout_seconds=timeout_seconds,
        )
        try:
            connection_context = self._client().connect()
            async with connection_context as connection:
                try:
                    try:
                        acquired = await connection.scalar(statement, parameters)
                    except DBAPIError as error:
                        if _mysql_error_number(error) in _CONTENTION_ERRORS:
                            raise AdvisoryLockUnavailableError(
                                "MySQL generation lock 未在等待预算内取得"
                            ) from error
                        raise
                    if not acquired:
                        raise AdvisoryLockUnavailableError(
                            "MySQL generation lock 未在等待预算内取得"
                        )
                    yield
                finally:
                    active_error = sys.exc_info()[1]
                    release_error = await self._release(connection)
                    if release_error is not None:
                        await _invalidate_owner_connection(connection, release_error)
                        if active_error is None:
                            raise AdvisoryLockReleaseError(
                                "MySQL generation lock 释放失败，owner 连接已失效"
                            ) from release_error
        except SQLAlchemyTimeoutError as error:
            raise AdvisoryLockUnavailableError(
                "MySQL generation lock owner 池已满"
            ) from error

    async def _release(self, connection: AsyncConnection) -> BaseException | None:
        """释放 owner 在 generation namespace 内的全部锁。"""
        try:
            released = await connection.scalar(
                text("SELECT service_release_locks(:namespace)"),
                {"namespace": _GENERATION_LOCK_NAMESPACE},
            )
            if not released:
                raise AdvisoryLockReleaseError("MySQL generation lock 未完整释放")
        except BaseException as error:
            return error
        return None

    async def check_capability(self) -> None:
        """在隔离 namespace 中探测 READ、WRITE 与 release 函数。"""
        probe = uuid4().hex
        try:
            async with self._client().connect() as connection:
                active_error: BaseException | None = None
                try:
                    try:
                        read_result = await connection.scalar(
                            text("SELECT service_get_read_locks(:namespace, :name, 0)"),
                            {"namespace": _PROBE_NAMESPACE, "name": f"read:{probe}"},
                        )
                        write_result = await connection.scalar(
                            text(
                                "SELECT service_get_write_locks(:namespace, :name, 0)"
                            ),
                            {"namespace": _PROBE_NAMESPACE, "name": f"write:{probe}"},
                        )
                    except DBAPIError as error:
                        raise LockingServiceUnavailableError(
                            "MySQL Locking Service SQL functions 不可用"
                        ) from error
                    if not read_result or not write_result:
                        raise LockingServiceUnavailableError(
                            "MySQL Locking Service capability probe 返回无效结果"
                        )
                except BaseException as error:
                    active_error = error
                    raise
                finally:
                    try:
                        released = await connection.scalar(
                            text("SELECT service_release_locks(:namespace)"),
                            {"namespace": _PROBE_NAMESPACE},
                        )
                        if not released:
                            raise LockingServiceUnavailableError(
                                "MySQL Locking Service release probe 返回无效结果"
                            )
                    except BaseException as release_error:
                        await _invalidate_owner_connection(connection, release_error)
                        if active_error is None:
                            raise LockingServiceUnavailableError(
                                "MySQL Locking Service SQL functions 不可用"
                            ) from release_error
        except SQLAlchemyTimeoutError as error:
            raise LockingServiceUnavailableError(
                "MySQL Locking Service capability probe 无可用 owner 连接"
            ) from error

    async def close(self) -> None:
        """幂等关闭并摘除专用 owner 引擎。"""
        engine = self._engine
        self._engine = None
        if engine is not None:
            await engine.dispose()
