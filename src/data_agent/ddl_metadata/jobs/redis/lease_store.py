"""DDL 任务来源租约与记忆变更互斥。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from redis.asyncio import Redis

from data_agent.ddl_metadata.jobs.redis.base import RedisBaseStore
from data_agent.ddl_metadata.jobs.redis.keys import JobKeys
from data_agent.ddl_metadata.jobs.redis.scripts import JobScripts
from data_agent.errors import DataAgentError
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
        # 步骤一：为本次浏览器变更生成唯一令牌，并映射到与图任务共享的来源键。
        token = f"mutation:{uuid4()}"
        key = self._keys.source(source)
        # 步骤二：以 NX 和短 TTL 获取临时租约，避免与活动 DDL 图并发修改同一来源。
        acquired = await RedisBaseStore.awaitable(
            self._redis.set(
                key,
                token,
                ex=min(app_config.memory.source_lease_seconds, 60),
                nx=True,
            )
        )
        # 步骤三：租约未取得时返回稳定冲突，禁止绕过来源级互斥继续写入。
        if not acquired:
            raise DataAgentError(
                "source_busy",
                "memory_mutation",
                "该逻辑数据源有活动任务",
                http_status=409,
            )
        # 步骤四：向调用方开放受保护区间，并在任何退出路径按令牌比较后释放，
        # 避免误删已经被其他所有者重新取得的租约。
        try:
            yield
        finally:
            await RedisBaseStore.awaitable(
                self._redis.eval(JobScripts.RELEASE, 1, key, token)
            )
