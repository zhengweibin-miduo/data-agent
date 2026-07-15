"""Qdrant 客户端生命周期管理。"""

from typing import ClassVar

from qdrant_client.async_qdrant_client import AsyncQdrantClient

from app.conf.app_config import app_config


class QdrantClientManager:
    """管理全局异步 Qdrant 客户端的初始化、获取与关闭。"""

    _client: ClassVar[AsyncQdrantClient | None] = None

    @classmethod
    def initialize(cls) -> AsyncQdrantClient:
        """初始化并返回 Qdrant 客户端，重复调用时复用现有实例。"""
        if cls._client is None:
            cls._client = AsyncQdrantClient(
                url=app_config.qdrant.url,
                api_key=app_config.qdrant.api_key,
            )

        return cls._client

    @classmethod
    def get_client(cls) -> AsyncQdrantClient:
        """返回已初始化的 Qdrant 客户端。"""
        if cls._client is None:
            raise RuntimeError(
                "Qdrant 客户端尚未初始化，请先调用 "
                "QdrantClientManager.initialize()"
            )

        return cls._client

    @classmethod
    async def close(cls) -> None:
        """关闭 Qdrant 客户端并清除当前实例。"""
        if cls._client is None:
            return

        await cls._client.close()
        cls._client = None
