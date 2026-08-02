"""DDL 任务 Redis Stream 事件边界检查。"""

from datetime import UTC, datetime
from typing import Any, cast

from redis.asyncio import Redis

from ddl_metadata.jobs.redis.event_store import JobEventStore
from ddl_metadata.jobs.redis.keys import JobKeys
from models.jobs import (
    JobEventData,
    JobEventStage,
    JobEventType,
    JobStatus,
)
from settings import app_config
from tests.helpers.checks import check_condition, check_equal


class _FakeRedis:
    """记录事件 Store 发出的 Redis 命令。"""

    def __init__(self, payload: str) -> None:
        """保存供读取返回的公开 JSON。"""
        self.payload = payload
        self.xadd_call: dict[str, Any] = {}
        self.expire_call: tuple[str, int] | None = None
        self.pipeline_transaction: bool | None = None

    def pipeline(self, *, transaction: bool) -> "_FakeRedis":
        """返回记录命令的事务流水线。"""
        self.pipeline_transaction = transaction
        return self

    def xadd(
        self,
        name: str,
        fields: dict[str, str],
        *,
        maxlen: int,
        approximate: bool,
    ) -> str:
        """记录有界 XADD 参数。"""
        self.xadd_call = {
            "name": name,
            "fields": fields,
            "maxlen": maxlen,
            "approximate": approximate,
        }
        return "queued"

    def expire(self, name: str, seconds: int) -> str:
        """记录 TTL 刷新参数。"""
        self.expire_call = (name, seconds)
        return "queued"

    async def execute(self) -> list[object]:
        """原子返回 XADD 与 EXPIRE 结果。"""
        return ["10-0", True]

    async def xrevrange(
        self,
        name: str,
        *,
        count: int,
    ) -> list[tuple[str, dict[str, str]]]:
        """返回当前尾事件。"""
        del name, count
        return [("10-0", {"event": "progress", "data": self.payload})]

    async def xread(
        self,
        streams: dict[str, str],
        *,
        count: int,
        block: int,
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        """返回游标后的单条公开事件。"""
        del count, block
        key = next(iter(streams))
        return [
            (
                key,
                [
                    (
                        "11-0",
                        {"event": "progress", "data": self.payload},
                    )
                ],
            )
        ]


async def test_event_store_bounds_retention_and_round_trips_contract() -> None:
    """事件追加受长度与 TTL 约束，读取时恢复严格模型。"""
    data = JobEventData(
        job_id="job-1",
        revision=0,
        attempt=1,
        status=JobStatus.RUNNING,
        stage=JobEventStage.PARSING,
        emitted_at=datetime(2026, 7, 19, tzinfo=UTC),
    )
    redis = _FakeRedis(data.model_dump_json())
    store = JobEventStore(cast(Redis, redis), JobKeys("ddl"))
    event_id = await store.publish("job-1", JobEventType.PROGRESS, data)
    check_equal("XADD 返回事件 ID", event_id, "10-0")
    check_equal("事务流水线", redis.pipeline_transaction, True)
    check_equal(
        "XADD 有界参数",
        {
            "name": redis.xadd_call["name"],
            "maxlen": redis.xadd_call["maxlen"],
            "approximate": redis.xadd_call["approximate"],
        },
        {
            "name": "ddl:job:job-1:events",
            "maxlen": app_config.redis.event_stream_max_events,
            "approximate": True,
        },
    )
    check_equal(
        "事件 Stream TTL",
        redis.expire_call,
        ("ddl:job:job-1:events", app_config.redis.result_retention_seconds),
    )
    check_condition(
        "事件字段排除 DDL",
        "ddl" not in redis.xadd_call["fields"]["data"].casefold(),
        actual=redis.xadd_call["fields"],
        expected="只保存公开事件类型和数据",
    )
    check_equal("尾事件 ID", await store.tail_id("job-1"), "10-0")
    events = await store.read_after(
        "job-1",
        "10-0",
        block_milliseconds=100,
    )
    check_equal("读取事件 ID", events[0].event_id, "11-0")
    check_equal("读取公开阶段", events[0].data.stage, JobEventStage.PARSING)
