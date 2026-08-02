"""真实 Redis 检查点与 MySQL 快照端到端检查。"""

from typing import cast
from uuid import uuid4

import pytest
from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from sqlalchemy import func, select

from ddl_metadata.adapters.mysql.accepted_snapshot import (
    MySQLAcceptedSnapshotPublisher,
)
from ddl_metadata.jobs.store import DDLJobStore
from ddl_metadata.persistence.tables import table_info
from ddl_metadata.workflow.contracts import DDLGraphDependencies
from ddl_metadata.workflow.graph import build_ddl_metadata_graph
from ddl_metadata.workflow.memory_context import MemoryContextLoader
from infrastructure.checkpoint_store import CheckpointStore
from infrastructure.mysql import MySQLDatabase
from infrastructure.redis import RedisClient
from memory.mysql.tables import agent_memory
from models.jobs import (
    AnswerRequest,
    DDLJobRequest,
    JobResult,
    JobStatus,
)
from models.semantic import (
    MetricAnswer,
    MetricQuestion,
)
from settings import app_config
from tests.helpers.checks import check_condition, check_equal
from tests.helpers.factories import cleanup_schema, ensure_schema
from tests.helpers.fakes import FakeMetadataGenerator

DDL = """
CREATE TABLE fact_integration (
    order_id BIGINT PRIMARY KEY,
    amount DECIMAL(10,2)
)
"""


async def _cleanup_job(
    jobs: DDLJobStore,
    job_id: str,
    source: str,
    revision: int,
) -> None:
    """清理当前集成任务 Redis 数据。"""
    redis = RedisClient.get_client()
    await redis.execute_command("DEL", jobs._job_key(job_id))
    await redis.execute_command("DEL", jobs._source_key(source))
    await redis.execute_command("ZREM", jobs.cleanup_key, job_id)
    for current_revision in range(revision + 1):
        member = f"{job_id}:{current_revision}"
        await redis.execute_command("ZREM", jobs.dispatch_key, member)
        await redis.execute_command("ZREM", jobs.waiting_key, member)
    await CheckpointStore.get_client().adelete_thread(job_id)


def _config(job_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": job_id}}


async def _test_flow() -> None:
    """完成提交、interrupt、回答、持久化及精确记忆复用。"""
    await ensure_schema()
    check_equal(
        "_test_flow 检查点 1",
        agent_memory.schema,
        app_config.memory.database,
    )
    check_condition(
        "_test_flow 检查点 2",
        table_info.schema is None,
        expected="原断言条件成立",
    )
    redis = RedisClient.initialize()
    checkpointer = await CheckpointStore.initialize()
    jobs = DDLJobStore(redis)
    source = f"integration_{uuid4().hex}"
    request = DDLJobRequest(source=source, ddl=DDL)
    schema = None
    created_jobs: list[tuple[str, int]] = []
    try:
        model = FakeMetadataGenerator()
        graph = build_ddl_metadata_graph(
            DDLGraphDependencies(
                model,
                MemoryContextLoader(),
                MySQLAcceptedSnapshotPublisher({source: "source_demo"}),
            ),
            checkpointer,
        )
        accepted = await jobs.submit(request)
        created_jobs.append((accepted.job_id, 1))
        check_condition(
            "_test_flow 检查点 3",
            await jobs.mark_running(accepted.job_id, 0),
            expected="原断言条件成立",
        )
        config = _config(accepted.job_id)
        await graph.ainvoke(
            {
                "job_id": accepted.job_id,
                "source": source,
                "dialect": "mysql",
                "ddl": DDL,
            },
            config,
            durability="sync",
        )
        snapshot = await graph.aget_state(config)
        interrupt_value = snapshot.tasks[0].interrupts[0].value
        questions = [
            MetricQuestion.model_validate(value)
            for value in interrupt_value["questions"]
        ]
        check_condition(
            "_test_flow 检查点 4",
            await jobs.mark_waiting(
                accepted.job_id,
                0,
                questions,
                int(interrupt_value["question_round"]),
            ),
            expected="原断言条件成立",
        )
        waiting = await jobs.get(accepted.job_id)
        check_condition(
            "_test_flow 检查点 5",
            waiting.question_set_id is not None,
            expected="原断言条件成立",
        )
        waiting_question_set_id = cast(str, waiting.question_set_id)
        answer = AnswerRequest(
            revision=0,
            question_set_id=waiting_question_set_id,
            answers=[
                MetricAnswer(
                    question_id=questions[0].question_id,
                    answer="SUM(amount) / COUNT(order_id), all rows, yuan",
                )
            ],
        )
        pending, first = await jobs.submit_answers(accepted.job_id, answer)
        check_condition(
            "_test_flow 检查点 6",
            first and pending.revision == 1,
            expected="原断言条件成立",
        )
        check_condition(
            "_test_flow 检查点 7",
            await jobs.mark_running(accepted.job_id, 1),
            expected="原断言条件成立",
        )
        output = await graph.ainvoke(
            Command(resume=[item.model_dump(mode="json") for item in answer.answers]),
            config,
            durability="sync",
        )
        result = JobResult.model_validate(output["result"])
        check_condition(
            "_test_flow 检查点 8",
            await jobs.mark_terminal(
                accepted.job_id,
                1,
                JobStatus.SUCCEEDED,
                result=result,
            ),
            expected="原断言条件成立",
        )
        check_equal(
            "_test_flow 检查点 9",
            (await jobs.get(accepted.job_id)).status,
            JobStatus.SUCCEEDED,
        )

        schema = output["physical_schema"]
        async with MySQLDatabase.session() as session:
            check_equal(
                "_test_flow 检查点 10",
                await session.scalar(
                    select(func.count())
                    .select_from(table_info)
                    .where(
                        table_info.c.id.in_([table["id"] for table in schema["tables"]])
                    )
                ),
                1,
            )
            check_equal(
                "_test_flow 检查点 11",
                await session.scalar(
                    select(func.count())
                    .select_from(agent_memory)
                    .where(agent_memory.c.source == source)
                ),
                4,
            )

        reuse_model = FakeMetadataGenerator()
        reuse_graph = build_ddl_metadata_graph(
            DDLGraphDependencies(
                reuse_model,
                MemoryContextLoader(),
                MySQLAcceptedSnapshotPublisher({source: "source_demo"}),
            ),
            checkpointer,
        )
        repeated = await jobs.submit(request)
        created_jobs.append((repeated.job_id, 0))
        check_condition(
            "_test_flow 检查点 12",
            await jobs.mark_running(repeated.job_id, 0),
            expected="原断言条件成立",
        )
        repeated_output = await reuse_graph.ainvoke(
            {
                "job_id": repeated.job_id,
                "source": source,
                "dialect": "mysql",
                "ddl": DDL,
            },
            _config(repeated.job_id),
            durability="sync",
        )
        check_equal(
            "_test_flow 检查点 13",
            repeated_output["status"],
            JobStatus.SUCCEEDED.value,
        )
        check_equal("_test_flow 检查点 14", reuse_model.classify_calls, 0)
        check_equal("_test_flow 检查点 15", reuse_model.question_calls, 0)
        check_equal("_test_flow 检查点 16", reuse_model.metric_calls, 0)
    finally:
        for job_id, revision in created_jobs:
            await _cleanup_job(jobs, job_id, source, revision)
        if schema is not None:
            from models.physical import PhysicalSchema

            await cleanup_schema(PhysicalSchema.model_validate(schema))
        await CheckpointStore.close()
        await MySQLDatabase.close()
        await RedisClient.close()


@pytest.mark.integration
async def test_ddl_metadata_flow() -> None:
    """运行端到端恢复流。"""
    await _test_flow()
