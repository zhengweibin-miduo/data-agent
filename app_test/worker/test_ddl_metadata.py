"""Redis 任务状态机、回答 CAS 和超时检查。"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from redis.exceptions import TimeoutError as RedisTimeoutError

from app.client.checkpoint_client_manager import CheckpointClientManager
from app.client.redis_client_manager import RedisClientManager
from app.model.ddl_metadata import (
    AnswerRequest,
    DdlJobRequest,
    JobStatus,
    MetricAnswer,
    MetricQuestion,
)
from app.service.ddl_metadata.errors import DdlMetadataError
from app.service.ddl_metadata.graph import (
    DdlGraphDependencies,
    build_ddl_metadata_graph,
)
from app.service.ddl_metadata.job_store import JobStore, question_set_id
from app.worker.ddl_metadata import (
    _RETRYABLE,
    cleanup_checkpoints,
    run_ddl_job,
)
from app_test.service.ddl_metadata.test_graph import (
    FakeMetadataModel,
    _NoMemory,
    _Snapshot,
)


async def _delete_job(store: JobStore, job_id: str, source: str) -> None:
    """只清理当前测试创建的 Redis 键与有序集合成员。"""
    redis = RedisClientManager.get_client()
    await redis.execute_command("DEL", store._job_key(job_id))
    await redis.execute_command("DEL", store._source_key(source))
    await redis.execute_command("ZREM", store.cleanup_key, job_id)
    for revision in range(3):
        member = f"{job_id}:{revision}"
        await redis.execute_command("ZREM", store.dispatch_key, member)
        await redis.execute_command("ZREM", store.waiting_key, member)


async def _test_job_state_machine() -> None:
    """验证受理、来源租约、等待、幂等回答和终态清理。"""
    redis = RedisClientManager.initialize()
    store = JobStore(redis)
    source = f"worker_{uuid4().hex}"
    request = DdlJobRequest(
        source=source,
        ddl="CREATE TABLE fact_test (id BIGINT PRIMARY KEY, amount INT)",
    )
    record = await store.submit(request)
    try:
        assert record.status == JobStatus.PENDING
        try:
            await store.submit(request)
        except DdlMetadataError as error:
            assert error.code == "source_busy"
        else:
            raise AssertionError("同一来源只能有一个活动任务")

        assert await store.mark_running(record.job_id, 0)
        question = MetricQuestion(
            question_id="metric.definition",
            prompt="Define metric",
            fact_table_id="fact-id",
            column_ids=["column-id"],
        )
        assert await store.mark_waiting(record.job_id, 0, [question], 1)
        waiting = await store.get(record.job_id)
        assert waiting.status == JobStatus.WAITING_INPUT
        assert waiting.question_set_id == question_set_id([question])
        assert waiting.question_set_id is not None
        assert waiting.expires_at is not None

        invalid_answer = AnswerRequest(
            revision=0,
            question_set_id=waiting.question_set_id,
            answers=[
                MetricAnswer(
                    question_id="unknown",
                    answer="SUM(amount)",
                )
            ],
        )
        try:
            await store.submit_answers(record.job_id, invalid_answer)
        except DdlMetadataError as error:
            assert error.code == "invalid_answers"
        else:
            raise AssertionError("回答不能引用当前轮次以外的问题")

        stale = AnswerRequest(
            revision=1,
            question_set_id=waiting.question_set_id,
            answers=[
                MetricAnswer(
                    question_id=question.question_id,
                    answer="SUM(amount)",
                )
            ],
        )
        try:
            await store.submit_answers(record.job_id, stale)
        except DdlMetadataError as error:
            assert error.code == "stale_answer"
        else:
            raise AssertionError("旧修订回答必须冲突")

        answer = stale.model_copy(update={"revision": 0})
        pending, accepted = await store.submit_answers(record.job_id, answer)
        assert accepted
        assert pending.status == JobStatus.PENDING
        assert pending.revision == 1
        repeated, accepted = await store.submit_answers(record.job_id, answer)
        assert not accepted
        assert repeated.revision == 1
        dispatch_count = await redis.execute_command(
            "ZCOUNT",
            store.dispatch_key,
            "-inf",
            "+inf",
        )
        assert dispatch_count is not None
        assert int(dispatch_count) >= 1

        assert await store.mark_running(record.job_id, 1)
        assert await store.mark_terminal(
            record.job_id,
            1,
            JobStatus.SUCCEEDED,
        )
        assert (await store.get(record.job_id)).status == JobStatus.SUCCEEDED
        assert (
            await redis.execute_command("GET", store._source_key(source))
            is None
        )
        assert (
            await redis.execute_command(
                "HMGET",
                store._job_key(record.job_id),
                "ddl",
                "answer_json",
                "questions_json",
            )
            == [None, None, None]
        )
    finally:
        await _delete_job(store, record.job_id, source)


async def _test_running_recovery() -> None:
    """验证崩溃后保留 running 投影的激活会从检查点继续。"""
    redis = RedisClientManager.get_client()
    store = JobStore(redis)
    source = f"recovery_{uuid4().hex}"
    record = await store.submit(
        DdlJobRequest(
            source=source,
            ddl=(
                "CREATE TABLE fact_test "
                "(id BIGINT PRIMARY KEY, amount INT)"
            ),
        )
    )
    try:
        assert await store.mark_running(record.job_id, 0)
        graph = build_ddl_metadata_graph(
            DdlGraphDependencies(
                FakeMetadataModel(),
                _NoMemory(),
                _Snapshot(),
            ),
            InMemorySaver(),
        )
        await run_ddl_job(
            {"jobs": store, "graph": graph},
            record.job_id,
            0,
        )
        recovered = await store.get(record.job_id)
        assert recovered.status == JobStatus.WAITING_INPUT
        assert recovered.revision == 0
    finally:
        await _delete_job(store, record.job_id, source)


async def _test_waiting_expiry() -> None:
    """验证显式截止时间扫尾，而不是依赖被动 TTL。"""
    redis = RedisClientManager.get_client()
    store = JobStore(redis)
    source = f"expiry_{uuid4().hex}"
    record = await store.submit(
        DdlJobRequest(
            source=source,
            ddl="CREATE TABLE fact_test (id BIGINT PRIMARY KEY, amount INT)",
        )
    )
    try:
        assert await store.mark_running(record.job_id, 0)
        question = MetricQuestion(
            question_id="metric.definition",
            prompt="Define metric",
            fact_table_id="fact-id",
        )
        assert await store.mark_waiting(record.job_id, 0, [question], 1)
        past = datetime.now(UTC).timestamp() - 1
        await redis.execute_command(
            "HSET",
            store._job_key(record.job_id),
            "expires_at_epoch",
            past,
        )
        await redis.execute_command(
            "ZADD",
            store.waiting_key,
            past,
            f"{record.job_id}:0",
        )
        expired = await store.expire_waiting()
        assert expired == [record.job_id]
        result = await store.get(record.job_id)
        assert result.status == JobStatus.REJECTED
        assert result.error is not None
        assert result.error.code == "answer_timeout"
        assert (
            await redis.execute_command(
                "HMGET",
                store._job_key(record.job_id),
                "ddl",
                "questions_json",
            )
            == [None, None]
        )
    finally:
        await _delete_job(store, record.job_id, source)


async def _test_answer_expiry_cleanup_outbox() -> None:
    """验证回答请求赢得超时竞态时仍安排检查点清理。"""
    redis = RedisClientManager.get_client()
    store = JobStore(redis)
    source = f"answer_expiry_{uuid4().hex}"
    record = await store.submit(
        DdlJobRequest(
            source=source,
            ddl="CREATE TABLE fact_test (id BIGINT PRIMARY KEY, amount INT)",
        )
    )
    try:
        assert await store.mark_running(record.job_id, 0)
        question = MetricQuestion(
            question_id="metric.definition",
            prompt="Define metric",
            fact_table_id="fact-id",
        )
        assert await store.mark_waiting(record.job_id, 0, [question], 1)
        waiting = await store.get(record.job_id)
        assert waiting.question_set_id is not None
        past = datetime.now(UTC).timestamp() - 1
        await redis.execute_command(
            "HSET",
            store._job_key(record.job_id),
            "expires_at_epoch",
            past,
        )
        try:
            await store.submit_answers(
                record.job_id,
                AnswerRequest(
                    revision=0,
                    question_set_id=waiting.question_set_id,
                    answers=[
                        MetricAnswer(
                            question_id=question.question_id,
                            answer="SUM(amount)",
                        )
                    ],
                ),
            )
        except DdlMetadataError as error:
            assert error.code == "answer_timeout"
        else:
            raise AssertionError("截止时间后的回答必须被拒绝")
        assert (
            await redis.execute_command(
                "ZSCORE",
                store.cleanup_key,
                record.job_id,
            )
            is not None
        )
    finally:
        await _delete_job(store, record.job_id, source)


async def _test_redis_durability_config() -> None:
    """验证本地 Redis 使用 AOF everysec。"""
    redis = RedisClientManager.get_client()
    appendonly = await redis.execute_command("CONFIG", "GET", "appendonly")
    appendfsync = await redis.execute_command("CONFIG", "GET", "appendfsync")
    assert appendonly == ["appendonly", "yes"]
    assert appendfsync == ["appendfsync", "everysec"]
    assert isinstance(RedisTimeoutError("timeout"), _RETRYABLE)


async def _test_checkpoint_cleanup_outbox() -> None:
    """验证检查点删除成功后才确认终态清理项。"""
    redis = RedisClientManager.get_client()
    store = JobStore(redis)
    job_id = f"cleanup-{uuid4()}"
    await redis.execute_command("ZADD", store.cleanup_key, 0, job_id)
    checkpointer = await CheckpointClientManager.initialize()
    with patch.object(
        type(checkpointer),
        "adelete_thread",
        new=AsyncMock(side_effect=RedisTimeoutError("timeout")),
    ):
        await cleanup_checkpoints({"jobs": store})
    assert (
        await redis.execute_command("ZSCORE", store.cleanup_key, job_id)
        is not None
    )

    await cleanup_checkpoints({"jobs": store})
    assert (
        await redis.execute_command("ZSCORE", store.cleanup_key, job_id)
        is None
    )


def test_ddl_metadata_worker() -> None:
    """运行真实 Redis 状态机检查。"""

    async def run() -> None:
        try:
            await _test_job_state_machine()
            await _test_running_recovery()
            await _test_waiting_expiry()
            await _test_answer_expiry_cleanup_outbox()
            await _test_redis_durability_config()
            await _test_checkpoint_cleanup_outbox()
        finally:
            await CheckpointClientManager.close()
            await RedisClientManager.close()

    asyncio.run(run())


if __name__ == "__main__":
    test_ddl_metadata_worker()
