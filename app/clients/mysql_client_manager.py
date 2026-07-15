"""MySQL 异步客户端生命周期管理。"""

from typing import ClassVar

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.conf.app_config import app_config


class MysqlClientManager:
    """管理全局 SQLAlchemy 异步引擎的初始化、获取与关闭。"""

    _client: ClassVar[AsyncEngine | None] = None

    @classmethod
    def initialize(cls) -> AsyncEngine:
        """初始化并返回异步引擎，重复调用时复用现有实例。"""
        if cls._client is None:
            cls._client = create_async_engine(app_config.mysql.url)

        return cls._client

    @classmethod
    def get_client(cls) -> AsyncEngine:
        """返回已初始化的异步引擎。"""
        if cls._client is None:
            raise RuntimeError(
                "MySQL 客户端尚未初始化，请先调用 MysqlClientManager.initialize()"
            )

        return cls._client

    @classmethod
    async def close(cls) -> None:
        """关闭异步引擎并清除当前实例。"""
        if cls._client is None:
            return

        await cls._client.dispose()
        cls._client = None
