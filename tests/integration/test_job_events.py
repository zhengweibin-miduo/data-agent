"""DDL 任务 Redis Stream 真实集成检查。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from data_agent.ddl_metadata.jobs.redis.event_store import JobEventStore
from data_agent.ddl_metadata.jobs.redis.keys import JobKeys
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.infrastructure.redis import RedisClient
from data_agent.models.jobs import (
    AnswerRequest,
    DDLJobRequest,
    JobEventData,
    JobEventStage,
    JobEventType,
    JobResult,
    JobStatus,
)
from data_agent.models.semantic import MetricAnswer, MetricQuestion
from data_agent.settings import app_config
from tests.helpers.checks import check_condition, check_equal

pytestmark = pytest.mark.integration


async def test_job_event_stream_round_trip_with_real_redis() -> None:
    """真实 Redis 中事件有序、有界并随任务状态保留。"""
    redis = RedisClient.initialize()
    store = DDLJobStore(redis)
    keys = JobKeys(app_config.redis.key_prefix)
    source = f"sse_{uuid4().hex}"
    bounded_job_id = f"bounded-{uuid4()}"
    record = await store.submit(
        DDLJobRequest(
            source=source,
            ddl="CREATE TABLE orders(id BIGINT PRIMARY KEY)",
        )
    )
    try:
        first = await store.read_events(
            record.job_id,
            "0-0",
            block_milliseconds=100,
        )
        check_equal("提交排队事件", first[0].data.stage, JobEventStage.QUEUED)
        check_condition(
            "排队事件排除原始 DDL",
            "CREATE TABLE" not in first[0].data.model_dump_json(),
            actual=first[0].data.model_dump(),
            expected="只含公开事件投影",
        )

        check_equal(
            "Pending -> running",
            await store.mark_running(record.job_id, record.revision),
            True,
        )
        await store.publish_progress(record.job_id, JobEventStage.PARSING)
        question = MetricQuestion(
            question_id="q-1",
            prompt="营业额口径？",
            fact_table_id="fact-1",
            column_ids=["amount"],
        )
        check_equal(
            "Running -> waiting_input",
            await store.mark_waiting(
                record.job_id,
                record.revision,
                [question],
                question_round=1,
            ),
            True,
        )
        waiting = await store.get(record.job_id)
        resumed, accepted = await store.submit_answers(
            record.job_id,
            AnswerRequest(
                revision=waiting.revision,
                question_set_id=waiting.question_set_id or "",
                answers=[
                    MetricAnswer(
                        question_id=question.question_id,
                        answer="SUM(amount), all rows, yuan",
                    )
                ],
            ),
        )
        check_equal("回答首次受理", accepted, True)
        check_equal("回答进入下一修订", resumed.revision, 1)
        check_equal(
            "下一修订 Pending -> running",
            await store.mark_running(record.job_id, resumed.revision),
            True,
        )
        check_equal(
            "Running -> succeeded",
            await store.mark_terminal(
                record.job_id,
                resumed.revision,
                JobStatus.SUCCEEDED,
                result=JobResult(
                    ddl_hash="sha256:test",
                    table_count=1,
                    column_count=1,
                    metric_count=0,
                ),
            ),
            True,
        )
        events = await store.read_events(
            record.job_id,
            first[-1].event_id,
            block_milliseconds=100,
        )
        check_equal(
            "进度至终态事件序列",
            [event.event_type for event in events],
            [
                JobEventType.PROGRESS,
                JobEventType.WAITING_INPUT,
                JobEventType.PROGRESS,
                JobEventType.SUCCEEDED,
            ],
        )
        check_equal(
            "等待事件公开问题",
            events[1].data.questions,
            [question],
        )
        check_equal("等待事件公开状态", events[1].data.status, JobStatus.WAITING_INPUT)
        check_equal("回答后排队事件修订", events[2].data.revision, resumed.revision)
        check_equal(
            "成功事件结果与权威投影一致",
            events[3].data.result,
            (await store.get(record.job_id)).result,
        )
        ttl = await redis.ttl(keys.events(record.job_id))
        check_condition(
            "事件 Stream TTL 有界",
            0 < ttl <= app_config.redis.result_retention_seconds,
            actual=ttl,
            expected="大于 0 且不超过结果保留期",
        )
        event_store = JobEventStore(redis, keys)
        overflow_count = app_config.redis.event_stream_max_events + 128
        overflow_data = JobEventData(
            job_id=bounded_job_id,
            revision=0,
            attempt=1,
            status=JobStatus.RUNNING,
            stage=JobEventStage.PARSING,
            emitted_at=datetime.now(UTC),
        )
        for _ in range(overflow_count):
            await event_store.publish(
                bounded_job_id,
                JobEventType.PROGRESS,
                overflow_data,
            )
        length = await redis.xlen(keys.events(bounded_job_id))
        check_condition(
            "事件 Stream 超阈值后发生近似裁剪",
            (
                length < overflow_count
                and length <= app_config.redis.event_stream_max_events + 100
            ),
            actual=length,
            expected=(
                f"小于写入量 {overflow_count} 且不超过近似上界 "
                f"{app_config.redis.event_stream_max_events + 100}"
            ),
        )
    finally:
        await _cleanup(redis, keys, record.job_id, source, bounded_job_id)
        await RedisClient.close()


async def _cleanup(
    redis: Redis,
    keys: JobKeys,
    job_id: str,
    source: str,
    bounded_job_id: str,
) -> None:
    """仅清理本测试创建的 Redis 键和 outbox 成员。"""
    await redis.delete(
        keys.job(job_id),
        keys.events(job_id),
        keys.events(bounded_job_id),
        keys.source(source),
    )
    await redis.zrem(keys.dispatch, keys.activation_member(job_id, 0))
    await redis.zrem(keys.dispatch, keys.activation_member(job_id, 1))
    await redis.zrem(
        keys.waiting,
        keys.activation_member(job_id, 0),
        keys.activation_member(job_id, 1),
    )
    await redis.zrem(keys.checkpoint_cleanup, job_id)
