"""Qdrant 客户端生命周期管理。"""

from typing import ClassVar

from qdrant_client.async_qdrant_client import AsyncQdrantClient

from data_agent.settings import app_config


class QdrantClient:
    """管理全局异步 Qdrant 客户端的初始化、获取与关闭。"""

    _client: ClassVar[AsyncQdrantClient | None] = None

    @classmethod
    def initialize(cls) -> AsyncQdrantClient:
        """初始化并返回 Qdrant 客户端，重复调用时复用现有实例。"""
        # 步骤一：以共享实例作为幂等门禁，已有客户端直接复用。
        if cls._client is None:
            # 步骤二：只从统一配置创建异步客户端，并保留可选认证兼容字段。
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
                "Qdrant 客户端尚未初始化，请先调用 QdrantClient.initialize()"
            )

        return cls._client

    @classmethod
    async def close(cls) -> None:
        """关闭 Qdrant 客户端并清除当前实例。"""
        # 步骤一：未初始化或已关闭时直接返回，保持关闭操作幂等。
        if cls._client is None:
            return

        # 步骤二：关闭当前客户端后清除共享引用，使后续初始化创建新实例。
        await cls._client.close()
        cls._client = None
