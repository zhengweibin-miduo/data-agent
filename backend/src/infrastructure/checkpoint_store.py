"""LangGraph Redis 检查点生命周期管理。"""

from typing import ClassVar

from langgraph.checkpoint.redis.aio import AsyncRedisSaver

from settings import app_config


class CheckpointStore:
    """显式初始化、设置并关闭异步 Redis 检查点。"""

    _client: ClassVar[AsyncRedisSaver | None] = None

    @classmethod
    async def initialize(cls) -> AsyncRedisSaver:
        """创建检查点客户端并初始化 Redis 索引。"""
        # 步骤一：以共享实例是否存在作为幂等门禁，避免重复进入异步资源上下文。
        if cls._client is None:
            # 步骤二：创建并显式进入 saver 上下文，使后续设置操作使用同一连接资源。
            # 该 saver 自建连接池，不复用 RedisClient，因此必须单独注入同一组套接字
            # 超时：否则 Redis 半开或收下请求后不再响应时，worker 会永久卡在
            # asetup() 的索引初始化或后续 checkpoint 读写上。
            client = AsyncRedisSaver(
                app_config.redis.url,
                connection_args={
                    "socket_timeout": app_config.redis.socket_timeout_seconds,
                    "socket_connect_timeout": (
                        app_config.redis.socket_connect_timeout_seconds
                    ),
                    "health_check_interval": (
                        app_config.redis.health_check_interval_seconds
                    ),
                },
                checkpoint_prefix=f"{app_config.redis.key_prefix}:checkpoint",
                checkpoint_write_prefix=(
                    f"{app_config.redis.key_prefix}:checkpoint_write"
                ),
            )
            await client.__aenter__()
            # 步骤三：初始化索引；失败时立即退出部分进入的上下文并保留原始异常。
            try:
                await client.asetup()
            except BaseException:
                await client.__aexit__(None, None, None)
                raise
            # 步骤四：仅在设置完整成功后发布共享实例，防止消费者取得半初始化客户端。
            cls._client = client
        return cls._client

    @classmethod
    def get_client(cls) -> AsyncRedisSaver:
        """返回已初始化的检查点客户端。"""
        if cls._client is None:
            raise RuntimeError(
                "检查点客户端尚未初始化，请先调用 CheckpointStore.initialize()"
            )
        return cls._client

    @classmethod
    async def close(cls) -> None:
        """关闭检查点客户端及其连接。"""
        # 步骤一：先摘除共享引用，使关闭期间的新初始化不会被旧资源覆盖。
        client = cls._client
        cls._client = None
        # 步骤二：仅退出本次捕获的 saver 上下文，未初始化或重复关闭保持幂等。
        if client is not None:
            await client.__aexit__(None, None, None)
