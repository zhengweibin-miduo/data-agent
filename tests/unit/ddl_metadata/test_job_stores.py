"""DDL 任务 Store 确定性边界检查。"""

from typing import cast

from redis.asyncio import Redis

from data_agent.ddl_metadata.jobs.ddl_job_store import DDLJobStore
from data_agent.ddl_metadata.jobs.job_codec_store import JobCodecStore
from data_agent.ddl_metadata.jobs.job_keys_store import JobKeysStore
from data_agent.ddl_metadata.models import (
    JobStatus,
    MetricAnswer,
    MetricQuestion,
)
from tests.helpers.checks import check_equal, check_exception, fail_check


def _question() -> MetricQuestion:
    """构造稳定的问题载荷。"""
    return MetricQuestion(
        question_id="q-1",
        prompt="营业额口径？",
        fact_table_id="fact-1",
        column_ids=["amount"],
    )


def test_job_keys_store_preserves_keyspace() -> None:
    """锁定任务键、成员和 arq 任务 ID 格式。"""
    keys = JobKeysStore("ddl")
    check_equal("任务 Hash 键", keys.job("job-1"), "ddl:job:job-1")
    check_equal("来源租约键", keys.source("source-1"), "ddl:source:source-1")
    check_equal("dispatch 键", keys.dispatch, "ddl:dispatch")
    check_equal("waiting 键", keys.waiting, "ddl:waiting")
    check_equal(
        "checkpoint cleanup 键",
        keys.checkpoint_cleanup,
        "ddl:checkpoint_cleanup",
    )
    check_equal(
        "激活成员",
        keys.activation_member("job-1", 2),
        "job-1:2",
    )
    check_equal(
        "arq 任务 ID",
        keys.arq_job_id("job-1", 2),
        "ddl:job-1:2",
    )


def test_job_codec_store_preserves_canonical_payloads() -> None:
    """锁定问题和回答的规范 JSON 与摘要。"""
    question = _question()
    answer = MetricAnswer(question_id="q-1", answer="SUM(amount)")
    check_equal(
        "问题存储 JSON",
        JobCodecStore.questions_json([question]),
        (
            '[{"question_id":"q-1","prompt":"营业额口径？",'
            '"fact_table_id":"fact-1","column_ids":["amount"],"required":true}]'
        ),
    )
    check_equal(
        "问题集合 ID",
        JobCodecStore.question_set_id([question]),
        "sha256:faf7078b5aeafcb8e1b6c0b6dee0bc9de39c448582a1d6e21c6612308c955cb9",
    )
    check_equal(
        "回答 JSON 与摘要",
        JobCodecStore.answers_payload([answer]),
        (
            '[{"answer":"SUM(amount)","question_id":"q-1"}]',
            "2cabdad656d38f3468a125d2f23b004144885bb3b2e8a82467b48e876d58c00e",
        ),
    )


def test_job_codec_store_projects_public_record() -> None:
    """验证 Hash 投影保留公开字段且忽略敏感字段。"""
    record = JobCodecStore.project(
        {
            "job_id": "job-1",
            "source": "source-1",
            "status": "waiting_input",
            "revision": "2",
            "attempt": "1",
            "question_round": "1",
            "question_set_id": "sha256:test",
            "questions_json": JobCodecStore.questions_json([_question()]),
            "created_at": "2026-07-19T00:00:00+00:00",
            "updated_at": "2026-07-19T00:01:00+00:00",
            "expires_at": "2026-07-19T00:06:00+00:00",
            "graph_version": "v1",
            "ddl": "CREATE TABLE secret(id INT)",
            "answer_json": '[{"answer":"secret"}]',
        }
    )
    check_equal("公开状态", record.status, JobStatus.WAITING_INPUT)
    check_equal("公开修订", record.revision, 2)
    check_equal("公开问题", record.questions, [_question()])
    check_equal(
        "公开投影字段",
        set(record.model_dump()),
        {
            "job_id",
            "source",
            "status",
            "revision",
            "attempt",
            "question_round",
            "question_set_id",
            "questions",
            "result",
            "error",
            "created_at",
            "updated_at",
            "expires_at",
            "graph_version",
        },
    )


async def test_ddl_job_store_rejects_illegal_transition_without_redis() -> None:
    """非法状态转换应在访问 Redis 前失败。"""
    store = DDLJobStore(cast(Redis, object()))
    try:
        await store.transition(
            "job-1",
            0,
            JobStatus.PENDING,
            JobStatus.SUCCEEDED,
        )
    except ValueError as error:
        check_exception("非法转换异常", error, ValueError)
        check_equal(
            "非法转换消息",
            str(error),
            "非法任务状态转换: pending->succeeded",
        )
    else:
        fail_check(
            "非法转换",
            actual="未抛出异常",
            expected="访问 Redis 前抛出 ValueError",
        )
