"""非终态 DDL 任务活动索引。"""

from typing import cast

from redis.asyncio import Redis

from data_agent.ddl_metadata.jobs.redis.base import RedisBaseStore
from data_agent.ddl_metadata.jobs.redis.keys import JobKeys


class JobActivityStore:
    """按最后推进时间读取与维护非终态任务索引。

    索引成员由 submit、transition 与 answer 的原子脚本维护，本 Store 只提供
    维护任务需要的读取、刷新与摘除操作，不参与状态转换裁决。
    """

    def __init__(self, redis: Redis, keys: JobKeys) -> None:
        """绑定 Redis 客户端与任务键空间。"""
        self._redis = redis
        self._keys = keys

    async def stalled(self, threshold: float, limit: int) -> list[str]:
        """读取最后推进时间早于阈值的有界候选任务。"""
        # 步骤一：只取越过停滞阈值的成员，索引按推进时间排序因而无需全量扫描。
        return cast(
            list[str],
            await RedisBaseStore.awaitable(
                self._redis.zrangebyscore(
                    self._keys.active,
                    min="-inf",
                    max=threshold,
                    start=0,
                    num=limit,
                )
            ),
        )

    async def touch(self, job_id: str, at: float) -> None:
        """把任务的最后推进时间前移，避免同一候选被连续重复巡检。"""
        await RedisBaseStore.awaitable(
            self._redis.zadd(self._keys.active, {job_id: at})
        )

    async def drop(self, job_id: str) -> None:
        """摘除已进入终态或已过保留期的索引成员。"""
        await RedisBaseStore.awaitable(self._redis.zrem(self._keys.active, job_id))
