"""DDL 元数据任务执行与恢复。"""

from __future__ import annotations

import json
import random
from time import perf_counter
from typing import Any, cast

from arq import Retry
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
    TimeoutError as RedisTimeoutError,
)
from sqlalchemy.exc import OperationalError

from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.ddl_metadata.worker.maintenance import cleanup_checkpoints
from data_agent.ddl_metadata.workflow.state import DDLGraphState
from data_agent.errors import DataAgentError
from data_agent.models.jobs import (
    JobError,
    JobEventStage,
    JobRecord,
    JobResult,
    JobStatus,
)
from data_agent.models.semantic import MetricQuestion
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

_NODE_STAGES = {
    "parse_ddl": JobEventStage.PARSING,
    "load_and_validate_memory": JobEventStage.MEMORY_LOADING,
    "classify_metadata": JobEventStage.METADATA_GENERATING,
    "validate_metadata": JobEventStage.METADATA_VALIDATING,
    "plan_metric_questions": JobEventStage.QUESTION_PLANNING,
    "generate_metrics": JobEventStage.METRIC_GENERATING,
    "validate_metrics": JobEventStage.METRIC_VALIDATING,
    "build_memory_candidates": JobEventStage.MEMORY_BUILDING,
    "persist_snapshot": JobEventStage.PERSISTING,
}


def _task_start_stage(event: object) -> JobEventStage | None:
    """只从 LangGraph v2 task-start 事件读取节点名并映射公开阶段。"""
    if not isinstance(event, dict) or event.get("type") != "tasks":
        return None
    data = event.get("data")
    if not isinstance(data, dict) or "input" not in data:
        return None
    name = data.get("name")
    return _NODE_STAGES.get(name) if isinstance(name, str) else None


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
    elif error is not None and not isinstance(error, DataAgentError):
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
        # 任务绑定的 graph_version 不允许由新图继续解释；PENDING 任务先通过
        # 修订保护进入 RUNNING，再按统一终态路径记录 attempt 并安排清理。
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
        # 同一 job_id 始终绑定同一 checkpoint 线程：无快照才注入原始请求，
        # interrupt 只恢复已提交回答，已完成快照先投影终态，其余情况续跑现有图。
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
        # sync durability 先固化节点结果再让 worker 继续；tasks 流只映射稳定阶段，
        # 节点输入、输出、interrupt 和错误均不得进入公开进度事件。
        async for event in graph.astream(
            graph_input,
            config,
            stream_mode="tasks",
            durability="sync",
            version="v2",
        ):
            stage = _task_start_stage(event)
            if stage is not None:
                await jobs.publish_progress(job_id, stage)
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
            # 仅显式瞬态异常可在预算内回到 PENDING；指数退避与 checkpoint 复用
            # 共同避免热循环，也避免持久化重试重复已经完成的模型节点。
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
                error.code if isinstance(error, DataAgentError) else "worker_failed"
            ),
            stage=(error.stage if isinstance(error, DataAgentError) else "worker"),
            retryable=False,
            attempt=latest.attempt,
            details=(
                error.details
                if isinstance(error, DataAgentError)
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
