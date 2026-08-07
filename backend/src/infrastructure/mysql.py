"""MySQL 异步客户端、Session 与命名锁生命周期管理。"""

import math
import sys
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import ClassVar
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from settings import app_config


class AdvisoryLockUnavailableError(RuntimeError):
    """MySQL 命名锁未在有限等待预算内取得。"""


class AdvisoryLockReleaseError(RuntimeError):
    """MySQL 命名锁未能由原连接可靠释放。"""


class LockingServiceUnavailableError(RuntimeError):
    """MySQL Locking Service SQL functions 不可用或返回形态错误。"""


_GENERATION_LOCK_NAMESPACE = "data-agent-generation-v1"
_LOCKING_SERVICE_PROBE_NAMESPACE = "data-agent-capability-v1"
_LOCKING_SERVICE_CONTENTION_ERRORS = frozenset({3132, 3133})


class MySQLDatabase:
    """管理全局 SQLAlchemy 异步引擎和独立事务 Session。"""

    _client: ClassVar[AsyncEngine | None] = None
    _session_factory: ClassVar[async_sessionmaker[AsyncSession] | None] = None

    @classmethod
    def initialize(cls) -> AsyncEngine:
        """初始化并返回异步引擎，重复调用时复用现有实例。"""
        # 步骤一：以共享引擎作为幂等门禁，避免重复创建连接池。
        if cls._client is None:
            # 步骤二：创建具备失效连接探测的引擎及绑定它的独立 Session 工厂。
            cls._client = create_async_engine(
                app_config.mysql.url,
                pool_pre_ping=True,
                pool_recycle=3600,
                connect_args={"init_command": "SET time_zone = '+00:00'"},
            )
            cls._session_factory = async_sessionmaker(
                bind=cls._client,
                expire_on_commit=False,
            )

        return cls._client

    @classmethod
    def get_client(cls) -> AsyncEngine:
        """返回已初始化的异步引擎。"""
        if cls._client is None:
            raise RuntimeError(
                "MySQL 客户端尚未初始化，请先调用 MySQLDatabase.initialize()"
            )

        return cls._client

    @classmethod
    @asynccontextmanager
    async def session(cls) -> AsyncIterator[AsyncSession]:
        """创建独立 Session，并自动提交、回滚和关闭。"""
        # 步骤一：捕获当前 Session 工厂，并在资源尚未初始化时立即拒绝事务。
        session_factory = cls._session_factory
        if session_factory is None:
            raise RuntimeError(
                "MySQL Session 尚未初始化，请先调用 MySQLDatabase.initialize()"
            )

        # 步骤二：每次进入上下文都创建独立 Session，避免并发任务共享事务状态。
        async with session_factory() as session:
            try:
                # 步骤三：把事务交给调用方，调用正常返回后统一提交。
                yield session
                await session.commit()
            except BaseException:
                # 步骤四：任何异常均回滚当前事务，并原样重新抛出供上层分类。
                await session.rollback()
                raise

    @classmethod
    @asynccontextmanager
    async def advisory_locks(
        cls,
        names: Iterable[str],
        *,
        timeout_seconds: float,
    ) -> AsyncIterator[None]:
        """在一条专用连接上按稳定顺序持有多个 MySQL advisory locks。

        Args:
            names: 需要共同持有的 MySQL 命名锁。
            timeout_seconds: 每把锁允许等待的有限秒数。

        Raises:
            AdvisoryLockUnavailableError: 任一命名锁未在预算内取得。
            AdvisoryLockReleaseError: 无业务异常时命名锁释放失败。
        """
        ordered_names = _normalize_advisory_lock_names(names)
        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError("MySQL advisory lock timeout 必须是有限非负秒数")

        # 步骤一：专用连接独立于业务事务，业务 commit 或 DDL auto-commit 不释放锁。
        async with cls.get_client().connect() as connection:
            acquired: list[str] = []
            try:
                # 步骤二：按 UTF-8 字节顺序取得全部锁，避免多目标发布互相死锁。
                for name in ordered_names:
                    result = await connection.scalar(
                        text("SELECT GET_LOCK(:lock_name, :timeout_seconds)"),
                        {
                            "lock_name": name,
                            "timeout_seconds": timeout_seconds,
                        },
                    )
                    if result != 1:
                        raise AdvisoryLockUnavailableError(
                            f"MySQL advisory lock 获取超时：{name}"
                        )
                    acquired.append(name)
                yield
            finally:
                # 步骤三：逆序释放已取得锁；释放异常先使连接失效，禁止带锁回池。
                active_error = sys.exc_info()[1]
                release_error = await _release_advisory_locks(connection, acquired)
                if release_error is not None:
                    await _invalidate_owner_connection(connection, release_error)
                    if active_error is None:
                        raise AdvisoryLockReleaseError(
                            "MySQL advisory lock 释放失败，owner 连接已失效"
                        ) from release_error

    @classmethod
    @asynccontextmanager
    async def shared_service_locks(
        cls,
        names: Iterable[str],
        *,
        timeout_seconds: int,
    ) -> AsyncIterator[None]:
        """在专用连接上原子持有共享 generation locks。"""
        async with cls._service_locks(
            names,
            function_name="service_get_read_locks",
            timeout_seconds=timeout_seconds,
        ):
            yield

    @classmethod
    @asynccontextmanager
    async def exclusive_service_locks(
        cls,
        names: Iterable[str],
        *,
        timeout_seconds: int,
    ) -> AsyncIterator[None]:
        """在专用连接上原子持有独占 generation locks。"""
        async with cls._service_locks(
            names,
            function_name="service_get_write_locks",
            timeout_seconds=timeout_seconds,
        ):
            yield

    @classmethod
    @asynccontextmanager
    async def _service_locks(
        cls,
        names: Iterable[str],
        *,
        function_name: str,
        timeout_seconds: int,
    ) -> AsyncIterator[None]:
        """通过单次 Locking Service 调用原子持有一组 generation locks。"""
        ordered_names = _normalize_advisory_lock_names(names)
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
        lock_arguments: list[str] = []
        for index, name in enumerate(ordered_names):
            parameter_name = f"lock_{index}"
            parameters[parameter_name] = name
            lock_arguments.append(f":{parameter_name}")
        statement = text(
            f"SELECT {function_name}(:namespace, "
            f"{', '.join(lock_arguments)}, :timeout_seconds)"
        )

        # 步骤一：每个上下文独占一条连接；单次函数调用保证多 target 原子获取。
        async with cls.get_client().connect() as connection:
            try:
                try:
                    acquired = await connection.scalar(statement, parameters)
                except DBAPIError as error:
                    if _mysql_error_number(error) in _LOCKING_SERVICE_CONTENTION_ERRORS:
                        raise AdvisoryLockUnavailableError(
                            "MySQL generation lock 未在等待预算内取得"
                        ) from error
                    raise
                if not acquired:
                    raise AdvisoryLockUnavailableError(
                        "MySQL generation lock 未在等待预算内取得"
                    )
                # 步骤二：锁跨业务 commit/rollback 保持，直至调用方离开临界区。
                yield
            finally:
                # 步骤三：namespace 级释放失败时使 owner 连接失效，保留活动业务异常。
                active_error = sys.exc_info()[1]
                release_error = await _release_service_locks(connection)
                if release_error is not None:
                    await _invalidate_owner_connection(connection, release_error)
                    if active_error is None:
                        raise AdvisoryLockReleaseError(
                            "MySQL generation lock 释放失败，owner 连接已失效"
                        ) from release_error

    @classmethod
    async def check_locking_service(cls) -> None:
        """探测 Locking Service SQL functions，缺失时阻断进程启动。"""
        # 步骤一：每个进程使用唯一资源名探测 READ 与 WRITE，避免并发启动互相竞争。
        probe_id = uuid4().hex
        async with cls.get_client().connect() as connection:
            try:
                try:
                    read_result = await connection.scalar(
                        text(
                            "SELECT service_get_read_locks("
                            ":namespace, :lock_name, 0)"
                        ),
                        {
                            "namespace": _LOCKING_SERVICE_PROBE_NAMESPACE,
                            "lock_name": f"read-probe:{probe_id}",
                        },
                    )
                    write_result = await connection.scalar(
                        text(
                            "SELECT service_get_write_locks("
                            ":namespace, :lock_name, 0)"
                        ),
                        {
                            "namespace": _LOCKING_SERVICE_PROBE_NAMESPACE,
                            "lock_name": f"write-probe:{probe_id}",
                        },
                    )
                except DBAPIError as error:
                    raise LockingServiceUnavailableError(
                        "MySQL Locking Service SQL functions 不可用"
                    ) from error
                if not read_result or not write_result:
                    raise LockingServiceUnavailableError(
                        "MySQL Locking Service capability probe 返回了无效结果"
                    )
            finally:
                # 步骤二：无论 probe 成败都释放隔离 namespace；失败连接禁止回池。
                active_error = sys.exc_info()[1]
                try:
                    released = await connection.scalar(
                        text("SELECT service_release_locks(:namespace)"),
                        {"namespace": _LOCKING_SERVICE_PROBE_NAMESPACE},
                    )
                    if not released:
                        raise LockingServiceUnavailableError(
                            "MySQL Locking Service release probe 返回了无效结果"
                        )
                except BaseException as release_error:
                    await _invalidate_owner_connection(connection, release_error)
                    if active_error is None:
                        if isinstance(
                            release_error, LockingServiceUnavailableError
                        ):
                            raise
                        raise LockingServiceUnavailableError(
                            "MySQL Locking Service SQL functions 不可用"
                        ) from release_error

    @classmethod
    async def close(cls) -> None:
        """关闭异步引擎并清除引擎与 Session 工厂。"""
        # 步骤一：在等待释放连接池前摘除两项共享状态，保留并发创建的替代实例。
        client = cls._client
        cls._client = None
        cls._session_factory = None

        # 步骤二：只释放本次捕获的旧引擎，未初始化或重复关闭保持幂等。
        if client is not None:
            await client.dispose()


def _normalize_advisory_lock_names(names: Iterable[str]) -> tuple[str, ...]:
    """校验、去重并按 UTF-8 字节顺序排列 MySQL 命名锁。"""
    unique = set(names)
    if not unique:
        raise ValueError("MySQL advisory lock names 不能为空")
    for name in unique:
        if not isinstance(name, str) or not name or len(name.encode("utf-8")) > 64:
            raise ValueError("MySQL advisory lock name 必须是 1 到 64 个 UTF-8 字节")
    return tuple(sorted(unique, key=lambda name: name.encode("utf-8")))


async def _release_advisory_locks(
    connection: AsyncConnection,
    acquired: list[str],
) -> BaseException | None:
    """逆序释放全部已取得锁并返回首个清理异常。"""
    release_error: BaseException | None = None
    for name in reversed(acquired):
        try:
            released = await connection.scalar(
                text("SELECT RELEASE_LOCK(:lock_name)"),
                {"lock_name": name},
            )
            if released != 1:
                raise AdvisoryLockReleaseError(
                    f"MySQL advisory lock 未由 owner 连接释放：{name}"
                )
        except BaseException as error:
            release_error = release_error or error
    return release_error


async def _release_service_locks(
    connection: AsyncConnection,
) -> BaseException | None:
    """释放 owner 连接在 generation namespace 内的全部锁。"""
    try:
        released = await connection.scalar(
            text("SELECT service_release_locks(:namespace)"),
            {"namespace": _GENERATION_LOCK_NAMESPACE},
        )
        if not released:
            raise AdvisoryLockReleaseError(
                "MySQL generation lock 未由 owner 连接完整释放"
            )
    except BaseException as error:
        return error
    return None


async def _invalidate_owner_connection(
    connection: AsyncConnection,
    error: BaseException,
) -> None:
    """使锁清理失败的 owner 连接失效，必要时直接关闭。"""
    try:
        await connection.invalidate(error)
    except BaseException:
        await connection.close()


def _mysql_error_number(error: DBAPIError) -> int | None:
    """从 SQLAlchemy 包装异常提取 MySQL server error number。"""
    arguments = getattr(error.orig, "args", ())
    if arguments and isinstance(arguments[0], int):
        return arguments[0]
    return None
