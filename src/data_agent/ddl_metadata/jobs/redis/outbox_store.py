"""DDL 任务 dispatch 与 checkpoint cleanup outbox。"""

from typing import cast

from arq.connections import ArqRedis
from redis.asyncio import Redis

from data_agent.ddl_metadata.jobs.redis.base import RedisBaseStore
from data_agent.ddl_metadata.jobs.redis.keys import JobKeys


class JobOutboxStore:
    """拥有 arq dispatch 与检查点清理 outbox。"""

    def __init__(self, redis: Redis, keys: JobKeys) -> None:
        """绑定 Redis 客户端与任务键空间。"""
        self._redis = redis
        self._keys = keys

    async def dispatch(self, queue: ArqRedis, limit: int = 100) -> int:
        """把 outbox 激活幂等写入 arq 后删除已处理项。"""
        members = cast(
            list[str],
            await RedisBaseStore.awaitable(
                self._redis.zrange(self._keys.dispatch, 0, limit - 1)
            ),
        )
        dispatched = 0
        for member in members:
            job_id, revision_text = member.rsplit(":", 1)
            await queue.enqueue_job(
                "run_ddl_job",
                job_id,
                int(revision_text),
                _job_id=self._keys.arq_job_id(job_id, revision_text),
            )
            await RedisBaseStore.awaitable(
                self._redis.zrem(self._keys.dispatch, member)
            )
            dispatched += 1
        return dispatched

    async def pending_checkpoint_cleanup(self, limit: int = 100) -> list[str]:
        """读取待删除的终态 LangGraph 线程。"""
        return cast(
            list[str],
            await RedisBaseStore.awaitable(
                self._redis.zrange(self._keys.checkpoint_cleanup, 0, limit - 1)
            ),
        )

    async def acknowledge_checkpoint_cleanup(self, job_id: str) -> None:
        """仅在检查点删除成功后确认清理 outbox。"""
        await RedisBaseStore.awaitable(
            self._redis.zrem(self._keys.checkpoint_cleanup, job_id)
        )
