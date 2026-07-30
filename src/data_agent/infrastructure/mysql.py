"""MySQL 异步客户端、Session 与命名锁生命周期管理。"""

import math
import sys
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import ClassVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from data_agent.settings import app_config


class AdvisoryLockUnavailableError(RuntimeError):
    """MySQL 命名锁未在有限等待预算内取得。"""


class AdvisoryLockReleaseError(RuntimeError):
    """MySQL 命名锁未能由原连接可靠释放。"""


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
                    try:
                        await connection.invalidate(release_error)
                    except BaseException:
                        await connection.close()
                    if active_error is None:
                        raise AdvisoryLockReleaseError(
                            "MySQL advisory lock 释放失败，owner 连接已失效"
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
