"""DDL 任务生命周期应用门面。"""

import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import ClassVar, cast
from uuid import uuid4

from arq.connections import ArqRedis
from loguru import logger
from redis.asyncio import Redis
from redis.exceptions import RedisError

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.jobs.identifiers import question_set_id
from data_agent.ddl_metadata.jobs.redis.base import RedisBaseStore
from data_agent.ddl_metadata.jobs.redis.codec import JobCodec
from data_agent.ddl_metadata.jobs.redis.event_store import JobEventStore
from data_agent.ddl_metadata.jobs.redis.keys import JobKeys
from data_agent.ddl_metadata.jobs.redis.lease_store import SourceLeaseStore
from data_agent.ddl_metadata.jobs.redis.outbox_store import JobOutboxStore
from data_agent.ddl_metadata.jobs.redis.state_store import RedisJobStateStore
from data_agent.ddl_metadata.models.jobs import (
    AnswerRequest,
    DDLJobRequest,
    JobError,
    JobEvent,
    JobEventData,
    JobEventStage,
    JobEventType,
    JobRecord,
    JobResult,
    JobStatus,
)
from data_agent.ddl_metadata.models.semantic import MetricQuestion
from data_agent.settings import app_config


class DDLJobStore:
    """组合专职 Store 并编排 DDL 任务业务生命周期。"""

    _prefix: ClassVar[str] = app_config.redis.key_prefix

    def __init__(self, redis: Redis) -> None:
        """绑定已初始化的应用 Redis 客户端。"""
        self._redis = redis
        self._keys = JobKeys(self._prefix)
        self._state = RedisJobStateStore(redis, self._keys)
        self._leases = SourceLeaseStore(redis, self._keys)
        self._outbox = JobOutboxStore(redis, self._keys)
        self._events = JobEventStore(redis, self._keys)

    @property
    def dispatch_key(self) -> str:
        """返回 dispatch outbox 键。"""
        return self._keys.dispatch

    @property
    def waiting_key(self) -> str:
        """返回等待截止时间有序集合键。"""
        return self._keys.waiting

    @property
    def cleanup_key(self) -> str:
        """返回终态检查点清理 outbox 键。"""
        return self._keys.checkpoint_cleanup

    def _job_key(self, job_id: str) -> str:
        """返回兼容测试使用的任务 Hash 键。"""
        return self._keys.job(job_id)

    def _source_key(self, source: str) -> str:
        """返回兼容测试使用的来源租约键。"""
        return self._keys.source(source)

    async def submit(self, request: DDLJobRequest) -> JobRecord:
        """原子写入任务和 outbox 后才报告受理。"""
        max_ddl_bytes = app_config.api.max_ddl_bytes
        if (
            len(request.ddl) > max_ddl_bytes
            or len(request.ddl.encode()) > max_ddl_bytes
        ):
            raise DDLMetadataError(
                "ddl_too_large",
                "submit",
                "DDL 超过配置的字节限制",
                http_status=422,
            )
        job_id = str(uuid4())
        accepted = await self._state.submit(job_id, request, datetime.now(UTC))
        if not accepted:
            raise DDLMetadataError(
                "source_busy",
                "submit",
                "该逻辑数据源已有活动任务",
                http_status=409,
            )
        record = await self.get(job_id)
        await self._publish_safely(
            record,
            JobEventType.PROGRESS,
            JobEventStage.QUEUED,
        )
        return record

    async def get(self, job_id: str) -> JobRecord:
        """读取公开安全任务投影。"""
        return await self._state.get(job_id)

    @staticmethod
    def snapshot_event(record: JobRecord, event_id: str) -> JobEvent:
        """从权威任务记录构造当前连接的初始快照。"""
        return JobEvent(
            event_id=event_id,
            event_type=JobEventType.SNAPSHOT,
            data=_event_data(record, _stage_for_status(record.status)),
        )

    async def event_tail_id(self, job_id: str) -> str:
        """读取任务公开事件流的当前尾游标。"""
        return await self._events.tail_id(job_id)

    async def read_events(
        self,
        job_id: str,
        after_id: str,
        *,
        block_milliseconds: int,
    ) -> list[JobEvent]:
        """在有界阻塞时间内读取指定游标后的公开事件。"""
        return await self._events.read_after(
            job_id,
            after_id,
            block_milliseconds=block_milliseconds,
        )

    async def publish_progress(
        self,
        job_id: str,
        stage: JobEventStage,
    ) -> None:
        """发布不包含节点输入、输出或错误的稳定业务进度。"""
        try:
            record = await self.get(job_id)
        except RedisError as error:
            _log_event_publish_failure(job_id, stage, error)
            return
        await self._publish_safely(record, JobEventType.PROGRESS, stage)

    async def execution_input(self, job_id: str) -> DDLJobRequest:
        """读取仅供 worker 使用的初始请求。"""
        return await self._state.execution_input(job_id)

    async def stored_answers(self, job_id: str) -> str | None:
        """读取当前修订的内部回答 JSON。"""
        return await self._state.stored_answers(job_id)

    async def transition(
        self,
        job_id: str,
        revision: int,
        expected: JobStatus,
        target: JobStatus,
        *,
        fields: Mapping[str, str] | None = None,
    ) -> bool:
        """按集中状态表执行修订感知的原子转换。"""
        if target not in _ALLOWED_TRANSITIONS[expected]:
            raise ValueError(f"非法任务状态转换: {expected}->{target}")
        changed = await self._state.transition(
            job_id,
            revision,
            expected,
            target,
            fields=fields,
        )
        if changed and target in _STATUS_EVENT_TYPES:
            await self._publish_current_safely(
                job_id,
                _STATUS_EVENT_TYPES[target],
                _stage_for_status(target),
            )
        return changed

    async def mark_running(self, job_id: str, revision: int) -> bool:
        """Pending -> running，并增加尝试次数。"""
        record = await self.get(job_id)
        return await self.transition(
            job_id,
            revision,
            JobStatus.PENDING,
            JobStatus.RUNNING,
            fields={"attempt": str(record.attempt + 1)},
        )

    async def mark_waiting(
        self,
        job_id: str,
        revision: int,
        questions: list[MetricQuestion],
        question_round: int,
    ) -> bool:
        """Running -> waiting_input，并登记显式截止时间。"""
        expires_at = datetime.now(UTC) + timedelta(
            seconds=app_config.redis.waiting_timeout_seconds
        )
        return await self.transition(
            job_id,
            revision,
            JobStatus.RUNNING,
            JobStatus.WAITING_INPUT,
            fields={
                "question_set_id": question_set_id(questions),
                "questions_json": JobCodec.questions_json(questions),
                "question_round": str(question_round),
                "expires_at": expires_at.isoformat(),
                "expires_at_epoch": str(expires_at.timestamp()),
            },
        )

    async def submit_answers(
        self,
        job_id: str,
        request: AnswerRequest,
    ) -> tuple[JobRecord, bool]:
        """原子验证回答并安排下一修订；返回是否首次受理。"""
        record = await self.get(job_id)
        if record.status == JobStatus.WAITING_INPUT:
            current_question_ids = {
                question.question_id for question in record.questions or []
            }
            answer_ids = [answer.question_id for answer in request.answers]
            duplicates = {
                answer_id for answer_id in answer_ids if answer_ids.count(answer_id) > 1
            }
            unknown = set(answer_ids) - current_question_ids
            if duplicates or unknown:
                raise DDLMetadataError(
                    "invalid_answers",
                    "waiting_input",
                    "回答必须唯一且只能引用当前问题",
                    http_status=422,
                    details={
                        "duplicate_ids": ",".join(sorted(duplicates)),
                        "unknown_ids": ",".join(sorted(unknown)),
                    },
                )
        answer_json, answer_hash = JobCodec.answers_payload(request.answers)
        result = await self._state.submit_answers(
            job_id,
            record.source,
            revision=request.revision,
            question_set_id=request.question_set_id,
            answer_json=answer_json,
            answer_hash=answer_hash,
        )
        if result == -1:
            raise DDLMetadataError(
                "answer_timeout",
                "waiting_input",
                "回答已超过截止时间",
                http_status=410,
            )
        if result == 0:
            raise DDLMetadataError(
                "stale_answer",
                "waiting_input",
                "回答修订或问题集合已过期",
                http_status=409,
            )
        updated = await self.get(job_id)
        if result == 1:
            await self._publish_safely(
                updated,
                JobEventType.PROGRESS,
                JobEventStage.QUEUED,
            )
        return updated, result == 1

    async def mark_terminal(
        self,
        job_id: str,
        revision: int,
        status: JobStatus,
        *,
        result: JobResult | None = None,
        error: JobError | None = None,
    ) -> bool:
        """Running -> 终态并释放来源租约、设置结果保留期。"""
        fields: dict[str, str] = {}
        if result is not None:
            fields["result_json"] = result.model_dump_json()
        if error is not None:
            fields["error_json"] = error.model_dump_json()
        return await self.transition(
            job_id,
            revision,
            JobStatus.RUNNING,
            status,
            fields=fields,
        )

    async def renew_source_lease(self, source: str, job_id: str) -> bool:
        """仅由当前所有者续期来源租约。"""
        return await self._leases.renew(source, job_id)

    @asynccontextmanager
    async def mutation_lease(self, source: str) -> AsyncIterator[None]:
        """短暂序列化浏览器记忆变更与活动图任务。"""
        async with self._leases.mutation(source):
            yield

    async def dispatch(self, queue: ArqRedis, limit: int = 100) -> int:
        """把 outbox 激活幂等写入 arq 后删除已处理项。"""
        return await self._outbox.dispatch(queue, limit)

    async def expire_waiting(self) -> list[str]:
        """拒绝已到期等待轮次；检查点清理由 worker cron 协调。"""
        now = time.time()
        members = cast(
            list[str],
            await RedisBaseStore.awaitable(
                self._redis.zrangebyscore(
                    self.waiting_key,
                    min="-inf",
                    max=now,
                )
            ),
        )
        expired: list[str] = []
        for member in members:
            job_id, revision_text = member.rsplit(":", 1)
            changed = await self.transition(
                job_id,
                int(revision_text),
                JobStatus.WAITING_INPUT,
                JobStatus.REJECTED,
                fields={
                    "error_json": JobError(
                        code="answer_timeout",
                        stage="waiting_input",
                        retryable=False,
                    ).model_dump_json()
                },
            )
            if changed:
                expired.append(job_id)
        return expired

    async def pending_checkpoint_cleanup(self, limit: int = 100) -> list[str]:
        """读取待删除的终态 LangGraph 线程。"""
        return await self._outbox.pending_checkpoint_cleanup(limit)

    async def acknowledge_checkpoint_cleanup(self, job_id: str) -> None:
        """仅在检查点删除成功后确认清理 outbox。"""
        await self._outbox.acknowledge_checkpoint_cleanup(job_id)

    async def _publish_current_safely(
        self,
        job_id: str,
        event_type: JobEventType,
        stage: JobEventStage,
    ) -> None:
        """读取转换后的权威投影并以非阻断副作用发布。"""
        try:
            record = await self.get(job_id)
        except RedisError as error:
            _log_event_publish_failure(job_id, stage, error)
            return
        await self._publish_safely(record, event_type, stage)

    async def _publish_safely(
        self,
        record: JobRecord,
        event_type: JobEventType,
        stage: JobEventStage,
    ) -> None:
        """发布失败只记录安全告警，不回滚已成功的业务状态。"""
        try:
            await self._events.publish(
                record.job_id,
                event_type,
                _event_data(record, stage),
            )
        except RedisError as error:
            _log_event_publish_failure(record.job_id, stage, error)


_ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.PENDING: {JobStatus.RUNNING},
    JobStatus.RUNNING: {
        JobStatus.PENDING,
        JobStatus.WAITING_INPUT,
        JobStatus.SUCCEEDED,
        JobStatus.REJECTED,
        JobStatus.FAILED,
    },
    JobStatus.WAITING_INPUT: {JobStatus.PENDING, JobStatus.REJECTED},
    JobStatus.SUCCEEDED: set(),
    JobStatus.REJECTED: set(),
    JobStatus.FAILED: set(),
}

_STATUS_EVENT_TYPES = {
    JobStatus.PENDING: JobEventType.PROGRESS,
    JobStatus.WAITING_INPUT: JobEventType.WAITING_INPUT,
    JobStatus.SUCCEEDED: JobEventType.SUCCEEDED,
    JobStatus.REJECTED: JobEventType.REJECTED,
    JobStatus.FAILED: JobEventType.FAILED,
}


def _stage_for_status(status: JobStatus) -> JobEventStage:
    """把公开状态映射为重连快照使用的稳定阶段。"""
    return {
        JobStatus.PENDING: JobEventStage.QUEUED,
        JobStatus.RUNNING: JobEventStage.RUNNING,
        JobStatus.WAITING_INPUT: JobEventStage.WAITING_INPUT,
        JobStatus.SUCCEEDED: JobEventStage.SUCCEEDED,
        JobStatus.REJECTED: JobEventStage.REJECTED,
        JobStatus.FAILED: JobEventStage.FAILED,
    }[status]


def _event_data(record: JobRecord, stage: JobEventStage) -> JobEventData:
    """只从公开 JobRecord 构造事件数据。"""
    return JobEventData(
        job_id=record.job_id,
        revision=record.revision,
        attempt=record.attempt,
        status=record.status,
        stage=stage,
        emitted_at=datetime.now(UTC),
        questions=(
            record.questions if record.status == JobStatus.WAITING_INPUT else None
        ),
        result=record.result if record.status == JobStatus.SUCCEEDED else None,
        error=(
            record.error
            if record.status
            in {JobStatus.REJECTED, JobStatus.FAILED}
            else None
        ),
    )


def _log_event_publish_failure(
    job_id: str,
    stage: JobEventStage,
    error: RedisError,
) -> None:
    """记录不包含任务输入和异常文本的安全事件写入告警。"""
    logger.bind(
        trace_id=job_id,
        component="ddl_metadata.jobs",
        event_name="ddl_metadata.job.event_publish_failed",
        operation="publish_job_event",
        outcome="degraded",
        stage=stage.value,
        retryable=True,
        error_type=type(error).__name__,
    ).warning("DDL 任务公开事件写入失败，将由权威快照修复")
