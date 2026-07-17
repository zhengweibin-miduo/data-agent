"""Redis 与 LangGraph 检查点客户端生命周期检查。"""

import asyncio

from app.client.checkpoint_client_manager import CheckpointClientManager
from app.client.redis_client_manager import RedisClientManager


async def _test_clients() -> None:
    """验证初始化保护、复用、真实连接、检查点设置和关闭。"""
    await CheckpointClientManager.close()
    await RedisClientManager.close()
    try:
        RedisClientManager.get_client()
    except RuntimeError as error:
        assert "RedisClientManager.initialize()" in str(error)
    else:
        raise AssertionError("未初始化时不应返回 Redis 客户端")

    redis = RedisClientManager.initialize()
    try:
        assert RedisClientManager.initialize() is redis
        assert RedisClientManager.get_client() is redis
        assert await redis.ping()
        checkpointer = await CheckpointClientManager.initialize()
        assert await CheckpointClientManager.initialize() is checkpointer
        assert CheckpointClientManager.get_client() is checkpointer
        await checkpointer.adelete_thread("client-manager-test")
    finally:
        await CheckpointClientManager.close()
        await RedisClientManager.close()

    replacement = RedisClientManager.initialize()
    try:
        assert replacement is not redis
        assert await replacement.ping()
    finally:
        await RedisClientManager.close()


def test_redis_client_manager() -> None:
    """运行真实 Redis 客户端检查。"""
    asyncio.run(_test_clients())


if __name__ == "__main__":
    test_redis_client_manager()
