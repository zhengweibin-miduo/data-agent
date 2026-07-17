"""arq worker、LangGraph 恢复协调和周期清理。"""

from __future__ import annotations

import json
import random
from typing import Any, cast

from arq import Retry, cron, func
from arq.connections import ArqRedis, RedisSettings
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot
from loguru import logger
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel
from redis.exceptions import (
    ConnectionError as RedisConnectionError,
)
from redis.exceptions import (
    RedisError,
)
from redis.exceptions import (
    TimeoutError as RedisTimeoutError,
)
from sqlalchemy.exc import OperationalError

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.ddl_metadata.memory.context import MemoryContextLoader
from data_agent.ddl_metadata.memory.snapshots import MetadataSnapshotService
from data_agent.ddl_metadata.models import (
    JobError,
    JobResult,
    JobStatus,
    MetricQuestion,
)
from data_agent.ddl_metadata.workflow.graph import (
    DDLGraphDependencies,
    DDLGraphState,
    build_ddl_metadata_graph,
)
from data_agent.ddl_metadata.workflow.metadata_generator import LLMMetadataGenerator
from data_agent.infrastructure.checkpoint_store import CheckpointStore
from data_agent.infrastructure.llm_client import LLMClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.redis import RedisClient
from data_agent.logging import setup_logging
from data_agent.settings import app_config

_RETRYABLE = (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
    OperationalError,
    RedisConnectionError,
    RedisTimeoutError,
    ConnectionError,
    TimeoutError,
)


class _InterruptProjection(BaseModel):
    """检查点 interrupt 的公开投影。"""

    questions: list[MetricQuestion]
    question_set_id: str
    question_round: int


def _interrupt_payload(
    snapshot: StateSnapshot,
) -> _InterruptProjection | None:
    """读取最新检查点中的首个 interrupt 安全载荷。"""
    for task in snapshot.tasks:
        for value in task.interrupts:
            if isinstance(value.value, dict):
                return _InterruptProjection.model_validate(value.value)
    return None


async def _project_snapshot(
    jobs: DDLJobStore,
    graph: CompiledStateGraph,
    job_id: str,
    revision: int,
    config: RunnableConfig,
) -> bool:
    """把检查点 interrupt/终态修复到公开 Redis 投影。"""
    snapshot = await graph.aget_state(config)
    interrupt_payload = _interrupt_payload(snapshot)
    if interrupt_payload is not None:
        questions = [
            MetricQuestion.model_validate(value)
            for value in interrupt_payload.questions
        ]
        await jobs.mark_waiting(
            job_id,
            revision,
            questions,
            interrupt_payload.question_round,
        )
        return True
    status_value = snapshot.values.get("status")
    if status_value == JobStatus.SUCCEEDED.value:
        result = JobResult.model_validate(snapshot.values["result"])
        await jobs.mark_terminal(
            job_id,
            revision,
            JobStatus.SUCCEEDED,
            result=result,
        )
        return True
    if status_value == JobStatus.REJECTED.value:
        record = await jobs.get(job_id)
        error = JobError.model_validate(snapshot.values["error"]).model_copy(
            update={"attempt": record.attempt}
        )
        await jobs.mark_terminal(
            job_id,
            revision,
            JobStatus.REJECTED,
            error=error,
        )
        return True
    return False


async def run_ddl_job(
    ctx: dict[Any, Any],
    job_id: str,
    revision: int,
) -> None:
    """执行或恢复一个公开任务修订。"""
    jobs = cast(DDLJobStore, ctx["jobs"])
    graph = cast(CompiledStateGraph, ctx["graph"])
    try:
        record = await jobs.get(job_id)
    except (
        RedisConnectionError,
        RedisTimeoutError,
        ConnectionError,
        TimeoutError,
    ) as error:
        raise Retry(defer=2) from error
    if record.status in {
        JobStatus.WAITING_INPUT,
        JobStatus.SUCCEEDED,
        JobStatus.REJECTED,
        JobStatus.FAILED,
    }:
        return
    if record.revision != revision:
        return
    if record.graph_version != app_config.llm.graph_version:
        if record.status == JobStatus.PENDING:
            await jobs.mark_running(job_id, revision)
        await jobs.mark_terminal(
            job_id,
            revision,
            JobStatus.FAILED,
            error=JobError(
                code="graph_version_mismatch",
                stage="worker",
                retryable=False,
                attempt=record.attempt,
            ),
        )
        await cleanup_checkpoints(ctx)
        return
    try:
        renewed = await jobs.renew_source_lease(record.source, job_id)
    except (
        RedisConnectionError,
        RedisTimeoutError,
        ConnectionError,
        TimeoutError,
    ) as error:
        raise Retry(defer=2) from error
    if not renewed:
        if record.status == JobStatus.PENDING and not await jobs.mark_running(
            job_id,
            revision,
        ):
            return
        await jobs.mark_terminal(
            job_id,
            revision,
            JobStatus.FAILED,
            error=JobError(
                code="source_lease_lost",
                stage="worker",
                retryable=False,
                attempt=record.attempt,
            ),
        )
        await cleanup_checkpoints(ctx)
        return
    if record.status == JobStatus.PENDING:
        if not await jobs.mark_running(job_id, revision):
            return
    elif record.status != JobStatus.RUNNING:
        return

    config: RunnableConfig = {
        "configurable": {"thread_id": job_id},
    }
    try:
        snapshot = await graph.aget_state(config)
        graph_input: DDLGraphState | Command | None
        interrupt_payload = _interrupt_payload(snapshot)
        if not snapshot.values:
            request = await jobs.execution_input(job_id)
            graph_input = {
                "job_id": job_id,
                "source": request.source,
                "dialect": request.dialect,
                "ddl": request.ddl,
            }
        elif interrupt_payload is not None:
            answer_json = await jobs.stored_answers(job_id)
            if answer_json is None:
                await _project_snapshot(
                    jobs,
                    graph,
                    job_id,
                    revision,
                    config,
                )
                return
            graph_input = Command(resume=json.loads(answer_json))
        elif not snapshot.next:
            if await _project_snapshot(
                jobs,
                graph,
                job_id,
                revision,
                config,
            ):
                await cleanup_checkpoints(ctx)
                return
            graph_input = None
        else:
            graph_input = None
        await graph.ainvoke(
            graph_input,
            config,
            durability="sync",
        )
        await _project_snapshot(
            jobs,
            graph,
            job_id,
            revision,
            config,
        )
        await cleanup_checkpoints(ctx)
    except Exception as error:
        try:
            latest = await jobs.get(job_id)
        except (
            RedisConnectionError,
            RedisTimeoutError,
            ConnectionError,
            TimeoutError,
        ) as redis_error:
            raise Retry(defer=2) from redis_error
        if isinstance(error, _RETRYABLE) and latest.attempt < 3:
            await jobs.transition(
                job_id,
                revision,
                JobStatus.RUNNING,
                JobStatus.PENDING,
            )
            raise Retry(defer=(2**latest.attempt) + random.uniform(0, 1)) from error
        await jobs.mark_terminal(
            job_id,
            revision,
            JobStatus.FAILED,
            error=JobError(
                code=(
                    error.code
                    if isinstance(error, DDLMetadataError)
                    else "worker_failed"
                ),
                stage=(
                    error.stage if isinstance(error, DDLMetadataError) else "worker"
                ),
                retryable=False,
                attempt=latest.attempt,
                details=(
                    error.details
                    if isinstance(error, DDLMetadataError)
                    else {"error_type": type(error).__name__}
                ),
            ),
        )
        await cleanup_checkpoints(ctx)


async def dispatch_pending(ctx: dict[Any, Any]) -> None:
    """启动及周期性排空 dispatch outbox。"""
    jobs = cast(DDLJobStore, ctx["jobs"])
    queue = cast(ArqRedis, ctx["redis"])
    await jobs.dispatch(queue)


async def expire_waiting(ctx: dict[Any, Any]) -> None:
    """周期性拒绝过期等待并删除对应检查点。"""
    jobs = cast(DDLJobStore, ctx["jobs"])
    await jobs.expire_waiting()
    await cleanup_checkpoints(ctx)


async def cleanup_checkpoints(ctx: dict[Any, Any]) -> None:
    """重试删除所有已进入终态的 LangGraph 线程。"""
    jobs = cast(DDLJobStore, ctx["jobs"])
    job_ids = await jobs.pending_checkpoint_cleanup()
    if not job_ids:
        return
    checkpointer = CheckpointStore.get_client()
    for job_id in job_ids:
        try:
            await checkpointer.adelete_thread(job_id)
        except RedisError as error:
            logger.bind(trace_id=job_id).warning(
                "终态检查点清理延后 error_type={}",
                type(error).__name__,
            )
            continue
        await jobs.acknowledge_checkpoint_cleanup(job_id)


async def startup(ctx: dict[Any, Any]) -> None:
    """显式初始化 worker 的全部长生命周期依赖。"""
    setup_logging()
    redis = RedisClient.initialize()
    MySQLDatabase.initialize()
    LLMClient.initialize()
    await LLMClient.check_structured_output_capability()
    checkpointer = await CheckpointStore.initialize()
    ctx["jobs"] = DDLJobStore(redis)
    ctx["graph"] = build_ddl_metadata_graph(
        DDLGraphDependencies(
            model=LLMMetadataGenerator(),
            memory_context=MemoryContextLoader(),
            snapshot=MetadataSnapshotService(),
        ),
        checkpointer,
    )
    await dispatch_pending(ctx)
    await cleanup_checkpoints(ctx)


async def shutdown(ctx: dict[Any, Any]) -> None:
    """按依赖逆序关闭 worker 资源。"""
    del ctx
    await CheckpointStore.close()
    await LLMClient.close()
    await MySQLDatabase.close()
    await RedisClient.close()


class WorkerSettings:
    """arq 可发现的 worker 设置。"""

    functions = [
        func(
            run_ddl_job,
            keep_result=0,
            timeout=app_config.redis.worker_job_timeout_seconds,
            max_tries=3,
        )
    ]
    cron_jobs = [
        cron(
            dispatch_pending,
            second={0, 10, 20, 30, 40, 50},
            run_at_startup=True,
        ),
        cron(expire_waiting, minute=None, second=0),
        cron(
            cleanup_checkpoints,
            second={5, 15, 25, 35, 45, 55},
        ),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(app_config.redis.url)
    max_jobs = app_config.redis.worker_concurrency
    job_timeout = app_config.redis.worker_job_timeout_seconds
    retry_jobs = True
    keep_result = 0
