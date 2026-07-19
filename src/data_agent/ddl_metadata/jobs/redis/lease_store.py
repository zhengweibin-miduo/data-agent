"""DDL 任务来源租约与记忆变更互斥。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from redis.asyncio import Redis

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.jobs.redis.base import RedisBaseStore
from data_agent.ddl_metadata.jobs.redis.keys import JobKeys
from data_agent.ddl_metadata.jobs.redis.scripts import JobScripts
from data_agent.settings import app_config


class SourceLeaseStore:
    """拥有任务来源租约续期和浏览器记忆变更临时租约。"""

    def __init__(self, redis: Redis, keys: JobKeys) -> None:
        """绑定 Redis 客户端与任务键空间。"""
        self._redis = redis
        self._keys = keys

    async def renew(self, source: str, job_id: str) -> bool:
        """仅由当前任务所有者续期来源租约。"""
        return bool(
            await RedisBaseStore.awaitable(
                self._redis.eval(
                    JobScripts.RENEW,
                    1,
                    self._keys.source(source),
                    job_id,
                    str(app_config.memory.source_lease_seconds),
                )
            )
        )

    @asynccontextmanager
    async def mutation(self, source: str) -> AsyncIterator[None]:
        """短暂序列化浏览器记忆变更与活动图任务。"""
        token = f"mutation:{uuid4()}"
        key = self._keys.source(source)
        acquired = await RedisBaseStore.awaitable(
            self._redis.set(
                key,
                token,
                ex=min(app_config.memory.source_lease_seconds, 60),
                nx=True,
            )
        )
        if not acquired:
            raise DDLMetadataError(
                "source_busy",
                "memory_mutation",
                "该逻辑数据源有活动任务",
                http_status=409,
            )
        try:
            yield
        finally:
            await RedisBaseStore.awaitable(
                self._redis.eval(JobScripts.RELEASE, 1, key, token)
            )
