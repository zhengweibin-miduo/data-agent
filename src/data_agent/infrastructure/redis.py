"""Redis 异步客户端生命周期管理。"""

from typing import ClassVar

from redis.asyncio import Redis

from data_agent.settings import app_config


class RedisClient:
    """管理应用任务状态使用的 Redis 客户端。"""

    _client: ClassVar[Redis | None] = None

    @classmethod
    def initialize(cls) -> Redis:
        """初始化并复用解码后的 Redis 客户端。"""
        # 步骤一：以共享实例作为幂等门禁，避免重复创建连接池。
        if cls._client is None:
            # 步骤二：从统一配置创建自动解码响应的异步客户端。
            cls._client = Redis.from_url(
                app_config.redis.url,
                decode_responses=True,
            )
        return cls._client

    @classmethod
    def get_client(cls) -> Redis:
        """返回已初始化的 Redis 客户端。"""
        if cls._client is None:
            raise RuntimeError(
                "Redis 客户端尚未初始化，请先调用 RedisClient.initialize()"
            )
        return cls._client

    @classmethod
    async def close(cls) -> None:
        """关闭当前 Redis 客户端。"""
        # 步骤一：先摘除共享引用，使关闭期间的新初始化不会被旧资源覆盖。
        client = cls._client
        cls._client = None
        # 步骤二：只关闭捕获实例的连接池，未初始化或重复关闭保持幂等。
        if client is not None:
            await client.aclose()
