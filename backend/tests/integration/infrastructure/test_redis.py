"""Redis 与 LangGraph 检查点客户端生命周期检查。"""

import pytest

from infrastructure.checkpoint_store import CheckpointStore
from infrastructure.redis import RedisClient
from tests.helpers.checks import check_condition, check_exception, fail_check


async def _test_clients() -> None:
    """验证初始化保护、复用、真实连接、检查点设置和关闭。"""
    await CheckpointStore.close()
    await RedisClient.close()
    try:
        RedisClient.get_client()
    except RuntimeError as error:
        check_exception("_test_clients 捕获预期异常", error, RuntimeError)
        check_condition(
            "_test_clients 检查点 1",
            "RedisClient.initialize()" in str(error),
            expected="原断言条件成立",
        )
    else:
        fail_check(
            "_test_clients",
            actual="未抛出预期异常",
            expected="未初始化时不应返回 Redis 客户端",
        )

    redis = RedisClient.initialize()
    try:
        check_condition(
            "_test_clients 检查点 2",
            RedisClient.initialize() is redis,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_clients 检查点 3",
            RedisClient.get_client() is redis,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_clients 检查点 4",
            await redis.ping(),
            expected="原断言条件成立",
        )
        checkpointer = await CheckpointStore.initialize()
        check_condition(
            "_test_clients 检查点 5",
            await CheckpointStore.initialize() is checkpointer,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_clients 检查点 6",
            CheckpointStore.get_client() is checkpointer,
            expected="原断言条件成立",
        )
        await checkpointer.adelete_thread("checkpoint-store-test")
    finally:
        await CheckpointStore.close()
        await RedisClient.close()

    replacement = RedisClient.initialize()
    try:
        check_condition(
            "_test_clients 检查点 7",
            replacement is not redis,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_clients 检查点 8",
            await replacement.ping(),
            expected="原断言条件成立",
        )
    finally:
        await RedisClient.close()


@pytest.mark.integration
async def test_redis_client() -> None:
    """运行真实 Redis 客户端检查。"""
    await _test_clients()
