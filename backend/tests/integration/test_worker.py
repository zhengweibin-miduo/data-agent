"""Redis 任务状态机、回答 CAS 和超时检查。"""

from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from redis.exceptions import TimeoutError as RedisTimeoutError

from ddl_metadata.jobs.identifiers import question_set_id
from ddl_metadata.jobs.store import DDLJobStore
from ddl_metadata.worker.job_runner import _RETRYABLE, run_ddl_job
from ddl_metadata.worker.maintenance import cleanup_checkpoints
from ddl_metadata.workflow.contracts import DDLGraphDependencies
from ddl_metadata.workflow.graph import build_ddl_metadata_graph
from errors import DataAgentError
from infrastructure.checkpoint_store import CheckpointStore
from infrastructure.redis import RedisClient
from models.jobs import (
    AnswerRequest,
    DDLJobRequest,
    JobError,
    JobStatus,
)
from models.semantic import (
    MetricAnswer,
    MetricQuestion,
)
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)
from tests.helpers.fakes import (
    FakeMetadataGenerator,
    _NoMemory,
    _Snapshot,
)


async def _delete_job(store: DDLJobStore, job_id: str, source: str) -> None:
    """只清理当前测试创建的 Redis 键与有序集合成员。"""
    redis = RedisClient.get_client()
    await redis.execute_command("DEL", store._job_key(job_id))
    await redis.execute_command("DEL", store._source_key(source))
    await redis.execute_command("ZREM", store.cleanup_key, job_id)
    for revision in range(3):
        member = f"{job_id}:{revision}"
        await redis.execute_command("ZREM", store.dispatch_key, member)
        await redis.execute_command("ZREM", store.waiting_key, member)


async def _test_job_state_machine() -> None:
    """验证受理、来源租约、等待、幂等回答和终态清理。"""
    redis = RedisClient.initialize()
    store = DDLJobStore(redis)
    source = f"worker_{uuid4().hex}"
    request = DDLJobRequest(
        source=source,
        ddl="CREATE TABLE fact_test (id BIGINT PRIMARY KEY, amount INT)",
    )
    record = await store.submit(request)
    try:
        check_equal(
            "_test_job_state_machine 检查点 1",
            record.status,
            JobStatus.PENDING,
        )
        try:
            await store.submit(request)
        except DataAgentError as error:
            check_exception(
                "_test_job_state_machine 捕获预期异常", error, DataAgentError
            )
            check_equal(
                "_test_job_state_machine 检查点 2",
                error.code,
                "source_busy",
            )
        else:
            fail_check(
                "_test_job_state_machine",
                actual="未抛出预期异常",
                expected="同一来源只能有一个活动任务",
            )

        check_condition(
            "_test_job_state_machine 检查点 3",
            await store.mark_running(record.job_id, 0),
            expected="原断言条件成立",
        )
        question = MetricQuestion(
            question_id="metric.definition",
            prompt="Define metric",
            fact_table_id="fact-id",
            column_ids=["column-id"],
        )
        check_condition(
            "_test_job_state_machine 检查点 4",
            await store.mark_waiting(record.job_id, 0, [question], 1),
            expected="原断言条件成立",
        )
        waiting = await store.get(record.job_id)
        check_equal(
            "_test_job_state_machine 检查点 5",
            waiting.status,
            JobStatus.WAITING_INPUT,
        )
        check_equal(
            "_test_job_state_machine 检查点 6",
            waiting.question_set_id,
            question_set_id([question]),
        )
        check_condition(
            "_test_job_state_machine 检查点 7",
            waiting.question_set_id is not None,
            expected="原断言条件成立",
        )
        waiting_question_set_id = cast(str, waiting.question_set_id)
        check_condition(
            "_test_job_state_machine 检查点 8",
            waiting.expires_at is not None,
            expected="原断言条件成立",
        )

        invalid_answer = AnswerRequest(
            revision=0,
            question_set_id=waiting_question_set_id,
            answers=[
                MetricAnswer(
                    question_id="unknown",
                    answer="SUM(amount)",
                )
            ],
        )
        try:
            await store.submit_answers(record.job_id, invalid_answer)
        except DataAgentError as error:
            check_exception(
                "_test_job_state_machine 捕获预期异常", error, DataAgentError
            )
            check_equal(
                "_test_job_state_machine 检查点 9",
                error.code,
                "invalid_answers",
            )
        else:
            fail_check(
                "_test_job_state_machine",
                actual="未抛出预期异常",
                expected="回答不能引用当前轮次以外的问题",
            )

        stale = AnswerRequest(
            revision=1,
            question_set_id=waiting_question_set_id,
            answers=[
                MetricAnswer(
                    question_id=question.question_id,
                    answer="SUM(amount)",
                )
            ],
        )
        try:
            await store.submit_answers(record.job_id, stale)
        except DataAgentError as error:
            check_exception(
                "_test_job_state_machine 捕获预期异常", error, DataAgentError
            )
            check_equal(
                "_test_job_state_machine 检查点 10",
                error.code,
                "stale_answer",
            )
        else:
            fail_check(
                "_test_job_state_machine",
                actual="未抛出预期异常",
                expected="旧修订回答必须冲突",
            )

        answer = stale.model_copy(update={"revision": 0})
        pending, accepted = await store.submit_answers(record.job_id, answer)
        check_condition(
            "_test_job_state_machine 检查点 11", accepted, expected="原断言条件成立"
        )
        check_equal(
            "_test_job_state_machine 检查点 12",
            pending.status,
            JobStatus.PENDING,
        )
        check_equal("_test_job_state_machine 检查点 13", pending.revision, 1)
        repeated, accepted = await store.submit_answers(record.job_id, answer)
        check_condition(
            "_test_job_state_machine 检查点 14",
            not accepted,
            expected="原断言条件成立",
        )
        check_equal("_test_job_state_machine 检查点 15", repeated.revision, 1)
        dispatch_count = await redis.execute_command(
            "ZCOUNT",
            store.dispatch_key,
            "-inf",
            "+inf",
        )
        check_condition(
            "_test_job_state_machine 检查点 16",
            dispatch_count is not None,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_job_state_machine 检查点 17",
            int(cast(str, dispatch_count)) >= 1,
            expected="原断言条件成立",
        )

        check_condition(
            "_test_job_state_machine 检查点 18",
            await store.mark_running(record.job_id, 1),
            expected="原断言条件成立",
        )
        check_condition(
            "_test_job_state_machine 检查点 19",
            await store.mark_terminal(
                record.job_id,
                1,
                JobStatus.SUCCEEDED,
            ),
            expected="原断言条件成立",
        )
        check_equal(
            "_test_job_state_machine 检查点 20",
            (await store.get(record.job_id)).status,
            JobStatus.SUCCEEDED,
        )
        check_condition(
            "_test_job_state_machine 检查点 21",
            await redis.execute_command("GET", store._source_key(source)) is None,
            expected="原断言条件成立",
        )
        check_equal(
            "_test_job_state_machine 检查点 22",
            await redis.execute_command(
                "HMGET",
                store._job_key(record.job_id),
                "ddl",
                "answer_json",
                "questions_json",
            ),
            [None, None, None],
        )
    finally:
        await _delete_job(store, record.job_id, source)


async def _test_running_recovery() -> None:
    """验证崩溃后保留 running 投影的激活会从检查点继续。"""
    redis = RedisClient.get_client()
    store = DDLJobStore(redis)
    source = f"recovery_{uuid4().hex}"
    record = await store.submit(
        DDLJobRequest(
            source=source,
            ddl=("CREATE TABLE fact_test (id BIGINT PRIMARY KEY, amount INT)"),
        )
    )
    try:
        check_condition(
            "_test_running_recovery 检查点 1",
            await store.mark_running(record.job_id, 0),
            expected="原断言条件成立",
        )
        graph = build_ddl_metadata_graph(
            DDLGraphDependencies(
                FakeMetadataGenerator(),
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
        check_equal(
            "_test_running_recovery 检查点 2",
            recovered.status,
            JobStatus.WAITING_INPUT,
        )
        check_equal("_test_running_recovery 检查点 3", recovered.revision, 0)
    finally:
        await _delete_job(store, record.job_id, source)


async def _test_waiting_expiry() -> None:
    """验证显式截止时间扫尾，而不是依赖被动 TTL。"""
    redis = RedisClient.get_client()
    store = DDLJobStore(redis)
    source = f"expiry_{uuid4().hex}"
    record = await store.submit(
        DDLJobRequest(
            source=source,
            ddl="CREATE TABLE fact_test (id BIGINT PRIMARY KEY, amount INT)",
        )
    )
    try:
        check_condition(
            "_test_waiting_expiry 检查点 1",
            await store.mark_running(record.job_id, 0),
            expected="原断言条件成立",
        )
        question = MetricQuestion(
            question_id="metric.definition",
            prompt="Define metric",
            fact_table_id="fact-id",
        )
        check_condition(
            "_test_waiting_expiry 检查点 2",
            await store.mark_waiting(record.job_id, 0, [question], 1),
            expected="原断言条件成立",
        )
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
        check_equal("_test_waiting_expiry 检查点 3", expired, [record.job_id])
        result = await store.get(record.job_id)
        check_equal(
            "_test_waiting_expiry 检查点 4",
            result.status,
            JobStatus.REJECTED,
        )
        check_condition(
            "_test_waiting_expiry 检查点 5",
            result.error is not None,
            expected="原断言条件成立",
        )
        check_equal(
            "_test_waiting_expiry 检查点 6",
            cast(JobError, result.error).code,
            "answer_timeout",
        )
        check_equal(
            "_test_waiting_expiry 检查点 7",
            await redis.execute_command(
                "HMGET",
                store._job_key(record.job_id),
                "ddl",
                "questions_json",
            ),
            [None, None],
        )
    finally:
        await _delete_job(store, record.job_id, source)


async def _test_answer_expiry_cleanup_outbox() -> None:
    """验证回答请求赢得超时竞态时仍安排检查点清理。"""
    redis = RedisClient.get_client()
    store = DDLJobStore(redis)
    source = f"answer_expiry_{uuid4().hex}"
    record = await store.submit(
        DDLJobRequest(
            source=source,
            ddl="CREATE TABLE fact_test (id BIGINT PRIMARY KEY, amount INT)",
        )
    )
    try:
        check_condition(
            "_test_answer_expiry_cleanup_outbox 检查点 1",
            await store.mark_running(record.job_id, 0),
            expected="原断言条件成立",
        )
        question = MetricQuestion(
            question_id="metric.definition",
            prompt="Define metric",
            fact_table_id="fact-id",
        )
        check_condition(
            "_test_answer_expiry_cleanup_outbox 检查点 2",
            await store.mark_waiting(record.job_id, 0, [question], 1),
            expected="原断言条件成立",
        )
        waiting = await store.get(record.job_id)
        check_condition(
            "_test_answer_expiry_cleanup_outbox 检查点 3",
            waiting.question_set_id is not None,
            expected="原断言条件成立",
        )
        waiting_question_set_id = cast(str, waiting.question_set_id)
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
                    question_set_id=waiting_question_set_id,
                    answers=[
                        MetricAnswer(
                            question_id=question.question_id,
                            answer="SUM(amount)",
                        )
                    ],
                ),
            )
        except DataAgentError as error:
            check_exception(
                "_test_answer_expiry_cleanup_outbox 捕获预期异常",
                error,
                DataAgentError,
            )
            check_equal(
                "_test_answer_expiry_cleanup_outbox 检查点 4",
                error.code,
                "answer_timeout",
            )
        else:
            fail_check(
                "_test_answer_expiry_cleanup_outbox",
                actual="未抛出预期异常",
                expected="截止时间后的回答必须被拒绝",
            )
        check_condition(
            "_test_answer_expiry_cleanup_outbox 检查点 5",
            await redis.execute_command(
                "ZSCORE",
                store.cleanup_key,
                record.job_id,
            )
            is not None,
            expected="原断言条件成立",
        )
    finally:
        await _delete_job(store, record.job_id, source)


async def _test_redis_durability_config() -> None:
    """验证本地 Redis 使用 AOF everysec。"""
    redis = RedisClient.get_client()
    appendonly = await redis.execute_command("CONFIG", "GET", "appendonly")
    appendfsync = await redis.execute_command("CONFIG", "GET", "appendfsync")
    check_equal(
        "_test_redis_durability_config 检查点 1",
        appendonly,
        ["appendonly", "yes"],
    )
    check_equal(
        "_test_redis_durability_config 检查点 2",
        appendfsync,
        ["appendfsync", "everysec"],
    )
    check_condition(
        "_test_redis_durability_config 检查点 3",
        isinstance(RedisTimeoutError("timeout"), _RETRYABLE),
        expected="原断言条件成立",
    )


async def _test_checkpoint_cleanup_outbox() -> None:
    """验证检查点删除成功后才确认终态清理项。"""
    redis = RedisClient.get_client()
    store = DDLJobStore(redis)
    job_id = f"cleanup-{uuid4()}"
    await redis.execute_command("ZADD", store.cleanup_key, 0, job_id)
    checkpointer = await CheckpointStore.initialize()
    with patch.object(
        type(checkpointer),
        "adelete_thread",
        new=AsyncMock(side_effect=RedisTimeoutError("timeout")),
    ):
        await cleanup_checkpoints({"jobs": store})
    check_condition(
        "_test_checkpoint_cleanup_outbox 检查点 1",
        await redis.execute_command("ZSCORE", store.cleanup_key, job_id) is not None,
        expected="原断言条件成立",
    )

    await cleanup_checkpoints({"jobs": store})
    check_condition(
        "_test_checkpoint_cleanup_outbox 检查点 2",
        await redis.execute_command("ZSCORE", store.cleanup_key, job_id) is None,
        expected="原断言条件成立",
    )


@pytest.mark.integration
async def test_ddl_metadata_worker() -> None:
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
            await CheckpointStore.close()
            await RedisClient.close()

    await run()
