"""非终态 DDL 任务活动索引。"""

from typing import cast

from redis.asyncio import Redis

from data_agent.ddl_metadata.jobs.redis.base import RedisBaseStore
from data_agent.ddl_metadata.jobs.redis.keys import JobKeys
from data_agent.models.jobs import JobStatus

_SCAN_BATCH = 200

_TERMINAL_STATUSES = frozenset(
    {
        JobStatus.SUCCEEDED.value,
        JobStatus.REJECTED.value,
        JobStatus.FAILED.value,
    }
)


class JobActivityStore:
    """按最后推进时间读取与维护非终态任务索引。

    索引成员由 submit、transition 与 answer 的原子脚本维护，本 Store 只提供
    维护任务需要的读取、刷新与摘除操作，不参与状态转换裁决。
    """

    def __init__(self, redis: Redis, keys: JobKeys) -> None:
        """绑定 Redis 客户端与任务键空间。"""
        self._redis = redis
        self._keys = keys

    async def now(self) -> float:
        """读取 Redis 服务端当前时间（秒）。

        活动索引的分数由 Lua 脚本用 `TIME` 写入，因此阈值比较与刷新都必须使用同一
        时钟。混用 worker 主机时钟会在两者偏移超过停滞宽限期时误判：仍在执行的任务
        被回退成 pending，其终态转换随后 CAS 失败；确定性 arq ID 又会让重新入队被
        去重，长任务因此可能反复进入这个循环。
        """
        seconds, _microseconds = cast(
            tuple[int, int],
            await RedisBaseStore.awaitable(self._redis.time()),
        )
        return float(seconds)

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

    async def backfill(self, at: float) -> int:
        """把键空间中所有非终态任务补录进活动索引。

        活动索引由 submit/transition/answer 脚本维护，因此只覆盖本版本受理的任务。
        升级前已经停在 pending/running 的任务、以及滚动发布期间由旧实例受理的任务
        都不在索引中；非终态任务 Hash 不设 TTL，不会自行消失，若不补录就永远不会被
        停滞巡检发现。每次 worker 启动执行一次，幂等且可重复。

        Args:
            at: 补录时写入的推进时间，应取 Redis 服务端时钟。

        Returns:
            本次补录的任务数量。
        """
        # 步骤一：按游标扫描任务 Hash 键，排除事件 Stream 等派生键。
        backfilled = 0
        cursor = 0
        while True:
            cursor, keys = cast(
                tuple[int, list[str]],
                await RedisBaseStore.awaitable(
                    self._redis.scan(
                        cursor=cursor,
                        match=f"{self._keys.prefix}:job:*",
                        count=_SCAN_BATCH,
                    )
                ),
            )
            for key in keys:
                if key.endswith(":events"):
                    continue
                # 步骤二：只补录仍处于非终态的任务；终态任务自带保留期 TTL。
                status = cast(
                    str | None,
                    await RedisBaseStore.awaitable(self._redis.hget(key, "status")),
                )
                if status is None or status in _TERMINAL_STATUSES:
                    continue
                job_id = key.rsplit(":", 1)[-1]
                await self.touch(job_id, at)
                backfilled += 1
            # 步骤三：游标回到 0 表示一轮完整扫描结束。
            if cursor == 0:
                return backfilled

    async def touch(self, job_id: str, at: float) -> None:
        """把任务的最后推进时间前移，避免同一候选被连续重复巡检。"""
        await RedisBaseStore.awaitable(
            self._redis.zadd(self._keys.active, {job_id: at})
        )

    async def drop(self, job_id: str) -> None:
        """摘除已进入终态或已过保留期的索引成员。"""
        await RedisBaseStore.awaitable(self._redis.zrem(self._keys.active, job_id))
