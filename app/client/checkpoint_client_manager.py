"""LangGraph Redis 检查点生命周期管理。"""

from typing import ClassVar

from langgraph.checkpoint.redis.aio import AsyncRedisSaver

from app.conf.app_config import app_config


class CheckpointClientManager:
    """显式初始化、设置并关闭异步 Redis 检查点。"""

    _client: ClassVar[AsyncRedisSaver | None] = None

    @classmethod
    async def initialize(cls) -> AsyncRedisSaver:
        """创建检查点客户端并初始化 Redis 索引。"""
        if cls._client is None:
            client = AsyncRedisSaver(
                app_config.redis.url,
                checkpoint_prefix=f"{app_config.redis.key_prefix}:checkpoint",
                checkpoint_write_prefix=(
                    f"{app_config.redis.key_prefix}:checkpoint_write"
                ),
            )
            await client.__aenter__()
            try:
                await client.asetup()
            except BaseException:
                await client.__aexit__(None, None, None)
                raise
            cls._client = client
        return cls._client

    @classmethod
    def get_client(cls) -> AsyncRedisSaver:
        """返回已初始化的检查点客户端。"""
        if cls._client is None:
            raise RuntimeError(
                "检查点客户端尚未初始化，请先调用 "
                "CheckpointClientManager.initialize()"
            )
        return cls._client

    @classmethod
    async def close(cls) -> None:
        """关闭检查点客户端及其连接。"""
        client = cls._client
        cls._client = None
        if client is not None:
            await client.__aexit__(None, None, None)
