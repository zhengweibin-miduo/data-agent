"""DDL 任务公开事件的有界 Redis Stream 存储。"""

from typing import cast

from redis.asyncio import Redis

from data_agent.ddl_metadata.jobs.redis.base import RedisBaseStore
from data_agent.ddl_metadata.jobs.redis.keys import JobKeys
from data_agent.models.jobs import JobEvent, JobEventData, JobEventType
from data_agent.settings import app_config


class JobEventStore:
    """拥有任务事件追加、尾游标和阻塞读取协议。"""

    def __init__(self, redis: Redis, keys: JobKeys) -> None:
        """绑定 Redis 客户端与任务键空间。"""
        self._redis = redis
        self._keys = keys

    async def publish(
        self,
        job_id: str,
        event_type: JobEventType,
        data: JobEventData,
    ) -> str:
        """追加有界公开事件并刷新与任务结果一致的 TTL。"""
        key = self._keys.events(job_id)
        pipeline = self._redis.pipeline(transaction=True)
        pipeline.xadd(
            key,
            {
                "event": event_type.value,
                "data": data.model_dump_json(),
            },
            maxlen=app_config.redis.event_stream_max_events,
            approximate=True,
        )
        pipeline.expire(
            key,
            app_config.redis.result_retention_seconds,
        )
        results = await pipeline.execute()
        return str(results[0])

    async def tail_id(self, job_id: str) -> str:
        """读取当前事件流尾 ID；尚无事件时返回 Redis 起始游标。"""
        entries = cast(
            list[tuple[str, dict[str, str]]],
            await RedisBaseStore.awaitable(
                self._redis.xrevrange(
                    self._keys.events(job_id),
                    count=1,
                )
            ),
        )
        return entries[0][0] if entries else "0-0"

    async def read_after(
        self,
        job_id: str,
        after_id: str,
        *,
        block_milliseconds: int,
        count: int = 100,
    ) -> list[JobEvent]:
        """在有界阻塞时间内读取指定游标后的公开事件。"""
        streams = cast(
            list[tuple[str, list[tuple[str, dict[str, str]]]]],
            await RedisBaseStore.awaitable(
                self._redis.xread(
                    {self._keys.events(job_id): after_id},
                    count=count,
                    block=block_milliseconds,
                )
            ),
        )
        if not streams:
            return []
        return [
            JobEvent(
                event_id=event_id,
                event_type=JobEventType(fields["event"]),
                data=JobEventData.model_validate_json(fields["data"]),
            )
            for _, entries in streams
            for event_id, fields in entries
        ]
