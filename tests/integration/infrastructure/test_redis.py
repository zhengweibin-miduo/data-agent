"""Redis 与 LangGraph 检查点客户端生命周期检查。"""

import pytest

from data_agent.infrastructure.checkpoint_store import CheckpointStore
from data_agent.infrastructure.redis import RedisClient


async def _test_clients() -> None:
    """验证初始化保护、复用、真实连接、检查点设置和关闭。"""
    await CheckpointStore.close()
    await RedisClient.close()
    try:
        RedisClient.get_client()
    except RuntimeError as error:
        assert "RedisClient.initialize()" in str(error)
    else:
        raise AssertionError("未初始化时不应返回 Redis 客户端")

    redis = RedisClient.initialize()
    try:
        assert RedisClient.initialize() is redis
        assert RedisClient.get_client() is redis
        assert await redis.ping()
        checkpointer = await CheckpointStore.initialize()
        assert await CheckpointStore.initialize() is checkpointer
        assert CheckpointStore.get_client() is checkpointer
        await checkpointer.adelete_thread("checkpoint-store-test")
    finally:
        await CheckpointStore.close()
        await RedisClient.close()

    replacement = RedisClient.initialize()
    try:
        assert replacement is not redis
        assert await replacement.ping()
    finally:
        await RedisClient.close()


@pytest.mark.integration
async def test_redis_client() -> None:
    """运行真实 Redis 客户端检查。"""
    await _test_clients()
