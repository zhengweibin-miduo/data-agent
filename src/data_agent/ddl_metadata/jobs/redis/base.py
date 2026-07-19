"""Redis 异步返回类型的窄化支持。"""

from collections.abc import Awaitable
from typing import TypeVar, cast

RedisResultT = TypeVar("RedisResultT")


class RedisBaseStore:
    """集中收窄 redis-py 同步与异步客户端共享的返回类型。"""

    @staticmethod
    def awaitable(
        value: Awaitable[RedisResultT] | RedisResultT,
    ) -> Awaitable[RedisResultT]:
        """将 redis-py 联合返回类型收窄为可等待结果。

        Args:
            value: redis-py 方法返回的同步值或可等待值。

        Returns:
            供异步 Store 等待的结果。
        """
        return cast(Awaitable[RedisResultT], value)
