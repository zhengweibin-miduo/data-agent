"""arq worker、LangGraph 恢复协调和周期清理。"""

from __future__ import annotations

import json
import random
from time import perf_counter
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
from data_agent.ddl_metadata.jobs.ddl_job_store import DDLJobStore
from data_agent.ddl_metadata.memory.context import MemoryContextLoader
from data_agent.ddl_metadata.memory.indexes import (
    MemoryElasticsearchIndex,
    MemoryQdrantIndex,
)
from data_agent.ddl_metadata.memory.outbox import MemoryIndexDispatcher
from data_agent.ddl_metadata.memory.snapshots import MetadataSnapshotService
from data_agent.ddl_metadata.models import (
    JobError,
    JobRecord,
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
from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.llm_client import LLMClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.infrastructure.redis import RedisClient
from data_agent.infrastructure.tei_embeddings import TEIEmbeddingClient
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


def _log_execution_outcome(
    record: JobRecord,
    started_at: float,
    *,
    error: Exception | None = None,
) -> None:
    """记录一次 worker 执行产生的完整公开结果。"""
    fields: dict[str, object] = {
        "trace_id": record.job_id,
        "component": "ddl_metadata.worker",
        "event_name": "ddl_metadata.job.execution.completed",
        "operation": "execute_job",
        "outcome": record.status.value,
        "job_status": record.status.value,
        "attempt": record.attempt,
        "revision": record.revision,
        "question_round": record.question_round,
        "duration_ms": max(0, round((perf_counter() - started_at) * 1000)),
    }
    if record.questions is not None:
        fields["question_count"] = len(record.questions)
    if record.result is not None:
        fields.update(
            table_count=record.result.table_count,
            column_count=record.result.column_count,
            metric_count=record.result.metric_count,
        )
    if record.error is not None:
        fields.update(
            error_code=record.error.code,
            stage=record.error.stage,
            retryable=record.error.retryable,
        )
        error_type = record.error.details.get("error_type")
        if error_type is not None:
            fields["error_type"] = error_type
    event_logger = logger.bind(**fields)
    if record.status in {JobStatus.SUCCEEDED, JobStatus.WAITING_INPUT}:
        event_logger.info("DDL 元数据任务执行完成")
    elif record.status == JobStatus.REJECTED:
        event_logger.warning("DDL 元数据任务已拒绝")
    elif error is not None and not isinstance(error, DDLMetadataError):
        event_logger.opt(exception=error).error("DDL 元数据任务执行失败")
    else:
        event_logger.error("DDL 元数据任务执行失败")


def _log_retry_scheduled(
    job_id: str,
    revision: int,
    attempt: int,
    error: Exception,
    *,
    job_status: JobStatus | None = None,
) -> None:
    """记录一次可恢复的 worker 重试安排。"""
    fields: dict[str, object] = {
        "trace_id": job_id,
        "component": "ddl_metadata.worker",
        "event_name": "ddl_metadata.job.retry_scheduled",
        "operation": "execute_job",
        "outcome": "retry_scheduled",
        "attempt": attempt,
        "revision": revision,
        "error_type": type(error).__name__,
        "retryable": True,
    }
    if job_status is not None:
        fields["job_status"] = job_status.value
    logger.bind(**fields).warning("DDL 元数据任务已安排重试")


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
    record: JobRecord,
    config: RunnableConfig,
) -> JobRecord | None:
    """把检查点 interrupt/终态修复到公开 Redis 投影。"""
    snapshot = await graph.aget_state(config)
    interrupt_payload = _interrupt_payload(snapshot)
    if interrupt_payload is not None:
        questions = [
            MetricQuestion.model_validate(value)
            for value in interrupt_payload.questions
        ]
        await jobs.mark_waiting(
            record.job_id,
            record.revision,
            questions,
            interrupt_payload.question_round,
        )
        return record.model_copy(
            update={
                "status": JobStatus.WAITING_INPUT,
                "questions": questions,
                "question_round": interrupt_payload.question_round,
            }
        )
    status_value = snapshot.values.get("status")
    if status_value == JobStatus.SUCCEEDED.value:
        result = JobResult.model_validate(snapshot.values["result"])
        await jobs.mark_terminal(
            record.job_id,
            record.revision,
            JobStatus.SUCCEEDED,
            result=result,
        )
        return record.model_copy(
            update={
                "status": JobStatus.SUCCEEDED,
                "result": result,
            }
        )
    if status_value == JobStatus.REJECTED.value:
        error = JobError.model_validate(snapshot.values["error"]).model_copy(
            update={"attempt": record.attempt}
        )
        await jobs.mark_terminal(
            record.job_id,
            record.revision,
            JobStatus.REJECTED,
            error=error,
        )
        return record.model_copy(
            update={
                "status": JobStatus.REJECTED,
                "error": error,
            }
        )
    return None


async def run_ddl_job(
    ctx: dict[Any, Any],
    job_id: str,
    revision: int,
) -> None:
    """执行或恢复一个公开任务修订。"""
    started_at = perf_counter()
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
        _log_retry_scheduled(job_id, revision, 0, error)
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
            record = record.model_copy(
                update={
                    "status": JobStatus.RUNNING,
                    "attempt": record.attempt + 1,
                }
            )
        job_error = JobError(
            code="graph_version_mismatch",
            stage="worker",
            retryable=False,
            attempt=record.attempt,
        )
        await jobs.mark_terminal(
            job_id,
            revision,
            JobStatus.FAILED,
            error=job_error,
        )
        _log_execution_outcome(
            record.model_copy(update={"status": JobStatus.FAILED, "error": job_error}),
            started_at,
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
        _log_retry_scheduled(
            job_id,
            revision,
            record.attempt,
            error,
            job_status=record.status,
        )
        raise Retry(defer=2) from error
    if not renewed:
        if record.status == JobStatus.PENDING and not await jobs.mark_running(
            job_id,
            revision,
        ):
            return
        if record.status == JobStatus.PENDING:
            record = record.model_copy(
                update={
                    "status": JobStatus.RUNNING,
                    "attempt": record.attempt + 1,
                }
            )
        job_error = JobError(
            code="source_lease_lost",
            stage="worker",
            retryable=False,
            attempt=record.attempt,
        )
        await jobs.mark_terminal(
            job_id,
            revision,
            JobStatus.FAILED,
            error=job_error,
        )
        _log_execution_outcome(
            record.model_copy(update={"status": JobStatus.FAILED, "error": job_error}),
            started_at,
        )
        await cleanup_checkpoints(ctx)
        return
    if record.status == JobStatus.PENDING:
        if not await jobs.mark_running(job_id, revision):
            return
        record = record.model_copy(
            update={
                "status": JobStatus.RUNNING,
                "attempt": record.attempt + 1,
            }
        )
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
                projected = await _project_snapshot(
                    jobs,
                    graph,
                    record,
                    config,
                )
                if projected is not None:
                    _log_execution_outcome(projected, started_at)
                return
            graph_input = Command(resume=json.loads(answer_json))
        elif not snapshot.next:
            projected = await _project_snapshot(
                jobs,
                graph,
                record,
                config,
            )
            if projected is not None:
                _log_execution_outcome(projected, started_at)
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
        projected = await _project_snapshot(
            jobs,
            graph,
            record,
            config,
        )
        if projected is not None:
            _log_execution_outcome(projected, started_at)
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
            _log_retry_scheduled(
                job_id,
                revision,
                record.attempt,
                redis_error,
                job_status=record.status,
            )
            raise Retry(defer=2) from redis_error
        if isinstance(error, _RETRYABLE) and latest.attempt < 3:
            await jobs.transition(
                job_id,
                revision,
                JobStatus.RUNNING,
                JobStatus.PENDING,
            )
            _log_retry_scheduled(
                job_id,
                revision,
                latest.attempt,
                error,
                job_status=JobStatus.PENDING,
            )
            raise Retry(defer=(2**latest.attempt) + random.uniform(0, 1)) from error
        job_error = JobError(
            code=(
                error.code if isinstance(error, DDLMetadataError) else "worker_failed"
            ),
            stage=(error.stage if isinstance(error, DDLMetadataError) else "worker"),
            retryable=False,
            attempt=latest.attempt,
            details=(
                error.details
                if isinstance(error, DDLMetadataError)
                else {"error_type": type(error).__name__}
            ),
        )
        await jobs.mark_terminal(
            job_id,
            revision,
            JobStatus.FAILED,
            error=job_error,
        )
        _log_execution_outcome(
            latest.model_copy(update={"status": JobStatus.FAILED, "error": job_error}),
            started_at,
            error=error,
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
            logger.bind(
                trace_id=job_id,
                component="ddl_metadata.worker",
                event_name="ddl_metadata.checkpoint.cleanup_deferred",
                operation="cleanup_checkpoint",
                outcome="deferred",
                error_type=type(error).__name__,
                retryable=True,
            ).warning("终态检查点清理延后")
            continue
        await jobs.acknowledge_checkpoint_cleanup(job_id)


async def dispatch_memory_index_outbox(ctx: dict[Any, Any]) -> None:
    """周期性同步可重建 ES/Qdrant 记忆投影。"""
    del ctx
    await MemoryIndexDispatcher().dispatch()


async def startup(ctx: dict[Any, Any]) -> None:
    """显式初始化 worker 的全部长生命周期依赖。"""
    setup_logging()
    redis = RedisClient.initialize()
    MySQLDatabase.initialize()
    elasticsearch = ElasticsearchClient.initialize()
    qdrant = QdrantClient.initialize()
    TEIEmbeddingClient.initialize()
    for target, setup in (
        (
            "ELASTICSEARCH",
            MemoryElasticsearchIndex(elasticsearch).setup,
        ),
        (
            "QDRANT",
            MemoryQdrantIndex(qdrant).setup,
        ),
    ):
        try:
            await setup()
        except Exception as error:
            logger.bind(
                trace_id="-",
                component="ddl_metadata.worker",
                event_name="ddl_metadata.memory.index_initialization_deferred",
                operation="setup_memory_index",
                outcome="deferred",
                stage=target.lower(),
                error_type=type(error).__name__,
                retryable=True,
            ).warning("记忆索引初始化延后")
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
    logger.bind(
        component="application.worker",
        event_name="application.lifecycle.started",
        operation="run_worker",
        outcome="started",
        worker_role="ddl_metadata",
    ).info("DDL 元数据 worker 已启动")


async def shutdown(ctx: dict[Any, Any]) -> None:
    """按依赖逆序关闭 worker 资源。"""
    del ctx
    await CheckpointStore.close()
    await LLMClient.close()
    await TEIEmbeddingClient.close()
    await QdrantClient.close()
    await ElasticsearchClient.close()
    await MySQLDatabase.close()
    await RedisClient.close()
    logger.bind(
        component="application.worker",
        event_name="application.lifecycle.stopped",
        operation="run_worker",
        outcome="stopped",
        worker_role="ddl_metadata",
    ).info("DDL 元数据 worker 已停止")


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
        cron(
            dispatch_memory_index_outbox,
            second={2, 12, 22, 32, 42, 52},
            run_at_startup=True,
        ),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(app_config.redis.url)
    max_jobs = app_config.redis.worker_concurrency
    job_timeout = app_config.redis.worker_job_timeout_seconds
    retry_jobs = True
    keep_result = 0
