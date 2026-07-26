"""DDL 元数据任务执行与恢复。"""

from __future__ import annotations

import json
import random
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
) -> None:
    """记录一次 worker 执行产生的完整公开结果。"""
    # 步骤一：只读取公开任务状态和安全错误码，不接触 graph 内部载荷。
    # 步骤二：按公开终态选择级别，链路和异常上下文由 worker 注册层的 AOP 补充。
    if record.status == JobStatus.SUCCEEDED:
        logger.info("DDL 元数据任务执行完成，公开结果已经持久化")
    elif record.status == JobStatus.WAITING_INPUT:
        logger.info("DDL 元数据任务已暂停，等待用户补充指标问题回答后继续执行")
    elif record.status == JobStatus.REJECTED:
        reason = record.error.code if record.error is not None else "business_rejected"
        logger.warning(f"DDL 元数据任务被业务规则拒绝，原因代码：{reason}")
    else:
        reason = record.error.code if record.error is not None else "worker_failed"
        logger.error(f"DDL 元数据任务执行失败，原因代码：{reason}")


def _log_retry_scheduled() -> None:
    """记录一次可恢复的 worker 重试安排。"""
    logger.warning("DDL 元数据任务遇到可恢复故障，已按退避策略安排自动重试")


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
    # 步骤一：读取当前 checkpoint，并优先把 interrupt 映射为等待输入投影。
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
    # 步骤二：没有 interrupt 时仅接受 graph 的明确终态，原子写入权威公开记录。
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
    # 步骤三：仍在执行的 checkpoint 不产生终态投影，由调用方继续流式运行。
    return None


async def run_ddl_job(
    ctx: dict[Any, Any],
    job_id: str,
    revision: int,
) -> None:
    """在公开状态与 checkpoint 之间执行或恢复一个任务修订。

    公开状态、revision 与来源租约阻止陈旧激活覆盖新状态，graph 版本另行
    阻止旧任务被不兼容的新图解释。同一 ``job_id`` 复用 checkpoint，使回答
    恢复和基础设施重试只续跑未完成节点；终态转换安排检查点异步清理。
    """
    jobs = cast(DDLJobStore, ctx["jobs"])
    graph = cast(CompiledStateGraph, ctx["graph"])
    # 步骤一：先读取权威公开记录；Redis 暂时不可用时不猜测状态，交由 arq 延迟重试。
    try:
        record = await jobs.get(job_id)
    except (
        RedisConnectionError,
        RedisTimeoutError,
        ConnectionError,
        TimeoutError,
    ) as error:
        _log_retry_scheduled()
        raise Retry(defer=2) from error
    # 步骤二：终态、等待输入和 revision 不匹配均说明本次激活已陈旧，直接退出。
    if record.status in {
        JobStatus.WAITING_INPUT,
        JobStatus.SUCCEEDED,
        JobStatus.REJECTED,
        JobStatus.FAILED,
    }:
        return
    if record.revision != revision:
        return
    # 步骤三：确认本次激活有效后立刻刷新活动索引的推进时间。arq 在任务超时后会
    # 自动重试同一激活，重试期间任务一直停留在 RUNNING、不产生任何状态转换；若不
    # 在每次激活开始时刷新，停滞阈值就会从**第一次**执行开始计时，第二次执行才跑
    # 了很短时间便被判为停滞并回退成 PENDING，使它随后的终态转换 CAS 失败。
    await jobs.heartbeat(job_id)
    # 步骤四：任务绑定的 graph_version 不允许由新图继续解释；PENDING 任务先通过
    # 修订保护进入 RUNNING，再按统一终态路径记录 attempt 并安排清理。
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
            record.model_copy(update={"status": JobStatus.FAILED, "error": job_error})
        )
        await cleanup_checkpoints(ctx)
        return
    # 步骤四：续租来源租约并以 revision 守卫进入 RUNNING，阻止陈旧执行覆盖新状态。
    try:
        renewed = await jobs.renew_source_lease(record.source, job_id)
    except (
        RedisConnectionError,
        RedisTimeoutError,
        ConnectionError,
        TimeoutError,
    ) as error:
        _log_retry_scheduled()
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
            record.model_copy(update={"status": JobStatus.FAILED, "error": job_error})
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
        # 步骤五：同一 job_id 始终绑定同一 checkpoint 线程：无快照才注入原始请求，
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
                    _log_execution_outcome(projected)
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
                _log_execution_outcome(projected)
                await cleanup_checkpoints(ctx)
                return
            graph_input = None
        else:
            graph_input = None
        # 步骤六：sync durability 先固化节点结果再让 worker 继续；tasks 流只映射
        # 稳定阶段，节点输入、输出、interrupt 和错误均不得进入公开进度事件。
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
            _log_execution_outcome(projected)
        await cleanup_checkpoints(ctx)
    except Retry:
        # 步骤七：图节点显式请求的 arq 重试属于 worker 控制流，原样交还调度器。
        raise
    except Exception as error:
        # 步骤八：重新读取权威 attempt；仅显式瞬态异常在预算内回到 PENDING。
        try:
            latest = await jobs.get(job_id)
        except (
            RedisConnectionError,
            RedisTimeoutError,
            ConnectionError,
            TimeoutError,
        ) as redis_error:
            _log_retry_scheduled()
            raise Retry(defer=2) from redis_error
        # 步骤九：可重试异常使用指数退避，并复用 checkpoint 避免重复已完成的模型节点。
        if isinstance(error, _RETRYABLE) and latest.attempt < 3:
            await jobs.transition(
                job_id,
                revision,
                JobStatus.RUNNING,
                JobStatus.PENDING,
            )
            _log_retry_scheduled()
            raise Retry(defer=(2**latest.attempt) + random.uniform(0, 1)) from error
        # 步骤十：不可重试或预算耗尽时写入安全失败终态，并安排 checkpoint 清理。
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
            latest.model_copy(update={"status": JobStatus.FAILED, "error": job_error})
        )
        await cleanup_checkpoints(ctx)
