"""真实 Redis 检查点与 MySQL 快照端到端检查。"""

import asyncio
from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command
from sqlalchemy import func, select

from app.client.checkpoint_client_manager import CheckpointClientManager
from app.client.mysql_client_manager import MysqlClientManager
from app.client.redis_client_manager import RedisClientManager
from app.conf.app_config import app_config
from app.model.ddl_metadata import (
    AnswerRequest,
    DdlJobRequest,
    JobResult,
    JobStatus,
    MetricAnswer,
    MetricQuestion,
)
from app.repository.ddl_metadata.schema import llm_memory, table_info
from app.service.ddl_metadata.graph import (
    DdlGraphDependencies,
    build_ddl_metadata_graph,
)
from app.service.ddl_metadata.job_store import JobStore
from app.service.ddl_metadata.memory_context import MemoryContextService
from app.service.ddl_metadata.memory import SnapshotService
from app_test.repository.ddl_metadata.fixtures import cleanup_schema, ensure_schema
from app_test.service.ddl_metadata.test_graph import FakeMetadataModel

DDL = """
CREATE TABLE fact_integration (
    order_id BIGINT PRIMARY KEY,
    amount DECIMAL(10,2)
)
"""


async def _cleanup_job(
    jobs: JobStore,
    job_id: str,
    source: str,
    revision: int,
) -> None:
    """清理当前集成任务 Redis 数据。"""
    redis = RedisClientManager.get_client()
    await redis.execute_command("DEL", jobs._job_key(job_id))
    await redis.execute_command("DEL", jobs._source_key(source))
    await redis.execute_command("ZREM", jobs.cleanup_key, job_id)
    for current_revision in range(revision + 1):
        member = f"{job_id}:{current_revision}"
        await redis.execute_command("ZREM", jobs.dispatch_key, member)
        await redis.execute_command("ZREM", jobs.waiting_key, member)
    await CheckpointClientManager.get_client().adelete_thread(job_id)


def _config(job_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": job_id}}


async def _test_flow() -> None:
    """完成提交、interrupt、回答、持久化及精确记忆复用。"""
    await ensure_schema()
    assert llm_memory.schema == app_config.memory.database
    assert table_info.schema is None
    redis = RedisClientManager.initialize()
    checkpointer = await CheckpointClientManager.initialize()
    jobs = JobStore(redis)
    source = f"integration_{uuid4().hex}"
    request = DdlJobRequest(source=source, ddl=DDL)
    schema = None
    created_jobs: list[tuple[str, int]] = []
    try:
        model = FakeMetadataModel()
        graph = build_ddl_metadata_graph(
            DdlGraphDependencies(
                model,
                MemoryContextService(),
                SnapshotService(),
            ),
            checkpointer,
        )
        accepted = await jobs.submit(request)
        created_jobs.append((accepted.job_id, 1))
        assert await jobs.mark_running(accepted.job_id, 0)
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
        assert await jobs.mark_waiting(
            accepted.job_id,
            0,
            questions,
            int(interrupt_value["question_round"]),
        )
        waiting = await jobs.get(accepted.job_id)
        assert waiting.question_set_id is not None
        answer = AnswerRequest(
            revision=0,
            question_set_id=waiting.question_set_id,
            answers=[
                MetricAnswer(
                    question_id=questions[0].question_id,
                    answer="SUM(amount) / COUNT(order_id), all rows, yuan",
                )
            ],
        )
        pending, first = await jobs.submit_answers(accepted.job_id, answer)
        assert first and pending.revision == 1
        assert await jobs.mark_running(accepted.job_id, 1)
        output = await graph.ainvoke(
            Command(
                resume=[
                    item.model_dump(mode="json") for item in answer.answers
                ]
            ),
            config,
            durability="sync",
        )
        result = JobResult.model_validate(output["result"])
        assert await jobs.mark_terminal(
            accepted.job_id,
            1,
            JobStatus.SUCCEEDED,
            result=result,
        )
        assert (await jobs.get(accepted.job_id)).status == JobStatus.SUCCEEDED

        schema = output["physical_schema"]
        async with MysqlClientManager.session() as session:
            assert (
                await session.scalar(
                    select(func.count()).select_from(table_info).where(
                        table_info.c.id.in_(
                            [table["id"] for table in schema["tables"]]
                        )
                    )
                )
                == 1
            )
            assert (
                await session.scalar(
                    select(func.count()).select_from(llm_memory).where(
                        llm_memory.c.source == source
                    )
                )
                or 0
            ) >= 6

        reuse_model = FakeMetadataModel()
        reuse_graph = build_ddl_metadata_graph(
            DdlGraphDependencies(
                reuse_model,
                MemoryContextService(),
                SnapshotService(),
            ),
            checkpointer,
        )
        repeated = await jobs.submit(request)
        created_jobs.append((repeated.job_id, 0))
        assert await jobs.mark_running(repeated.job_id, 0)
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
        assert repeated_output["status"] == JobStatus.SUCCEEDED.value
        assert reuse_model.classify_calls == 0
        assert reuse_model.question_calls == 0
        assert reuse_model.metric_calls == 0
    finally:
        for job_id, revision in created_jobs:
            await _cleanup_job(jobs, job_id, source, revision)
        if schema is not None:
            from app.model.ddl_metadata import PhysicalSchema

            await cleanup_schema(PhysicalSchema.model_validate(schema))
        await CheckpointClientManager.close()
        await MysqlClientManager.close()
        await RedisClientManager.close()


def test_ddl_metadata_flow() -> None:
    """运行端到端恢复流。"""
    asyncio.run(_test_flow())


if __name__ == "__main__":
    test_ddl_metadata_flow()
