"""MySQL 异步客户端与 Session 生命周期管理。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import ClassVar

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from data_agent.settings import app_config


class MySQLDatabase:
    """管理全局 SQLAlchemy 异步引擎和独立事务 Session。"""

    _client: ClassVar[AsyncEngine | None] = None
    _session_factory: ClassVar[async_sessionmaker[AsyncSession] | None] = None

    @classmethod
    def initialize(cls) -> AsyncEngine:
        """初始化并返回异步引擎，重复调用时复用现有实例。"""
        if cls._client is None:
            cls._client = create_async_engine(
                app_config.mysql.url,
                pool_pre_ping=True,
                pool_recycle=3600,
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
        session_factory = cls._session_factory
        if session_factory is None:
            raise RuntimeError(
                "MySQL Session 尚未初始化，请先调用 MySQLDatabase.initialize()"
            )

        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    @classmethod
    async def close(cls) -> None:
        """关闭异步引擎并清除引擎与 Session 工厂。"""
        client = cls._client
        cls._client = None
        cls._session_factory = None

        if client is not None:
            await client.dispose()
