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

from data_agent.ddl_metadata.jobs.identifiers import question_set_id
from data_agent.ddl_metadata.jobs.recovery import StallAction, stall_action
from data_agent.ddl_metadata.jobs.redis.activity_store import JobActivityStore
from data_agent.ddl_metadata.jobs.redis.base import RedisBaseStore
from data_agent.ddl_metadata.jobs.redis.codec import JobCodec
from data_agent.ddl_metadata.jobs.redis.event_store import JobEventStore
from data_agent.ddl_metadata.jobs.redis.keys import JobKeys
from data_agent.ddl_metadata.jobs.redis.lease_store import SourceLeaseStore
from data_agent.ddl_metadata.jobs.redis.outbox_store import JobOutboxStore
from data_agent.ddl_metadata.jobs.redis.state_store import RedisJobStateStore
from data_agent.errors import DataAgentError
from data_agent.models.jobs import (
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
from data_agent.models.semantic import MetricQuestion
from data_agent.settings import app_config


class DDLJobStore:
    """组合专职 Store 并编排 DDL 任务业务生命周期。"""

    _prefix: ClassVar[str] = app_config.redis.key_prefix

    def __init__(self, redis: Redis, queue: ArqRedis | None = None) -> None:
        """绑定已初始化的应用 Redis 客户端与可选的 arq 队列。

        Args:
            redis: 应用共享的 Redis 客户端。
            queue: 可选 arq 队列。提供时受理路径会立即调度激活，把等待 dispatch
                cron 的时延降到零；不提供时完全退回 outbox + cron 的既有行为。
        """
        self._redis = redis
        self._queue = queue
        self._keys = JobKeys(self._prefix)
        self._state = RedisJobStateStore(redis, self._keys)
        self._leases = SourceLeaseStore(redis, self._keys)
        self._outbox = JobOutboxStore(redis, self._keys)
        self._events = JobEventStore(redis, self._keys)
        self._activity = JobActivityStore(redis, self._keys)

    async def _activate_now_safely(self, job_id: str, revision: int) -> None:
        """尽力立即调度一个激活请求，失败不影响已成立的受理承诺。

        outbox 成员仍然是崩溃兜底与唯一的可恢复调度请求，因此这里的失败只会让
        激活退回到 cron 周期，不需要撤销受理，也不应向调用方暴露。
        """
        # 步骤一：未注入队列时保持既有 outbox + cron 行为。
        if self._queue is None:
            return
        # 步骤二：立即调度失败只记录告警；outbox 成员未被移除，cron 会重放。
        try:
            await self._outbox.dispatch_one(self._queue, job_id, revision)
        except (RedisError, OSError):
            _log_immediate_dispatch_deferred(job_id)

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
        """原子建立任务、来源租约和 dispatch outbox 后才报告受理。

        权威 Hash 与可恢复调度请求先提交；公开事件属于尽力通知，即使发布
        失败也由后续读取权威投影修复，而不会撤销已经成立的受理承诺。
        """
        # 步骤一：先按 UTF-8 字节上限拒绝过大 DDL，避免为不可受理请求创建状态。
        max_ddl_bytes = app_config.api.max_ddl_bytes
        if (
            len(request.ddl) > max_ddl_bytes
            or len(request.ddl.encode()) > max_ddl_bytes
        ):
            raise DataAgentError(
                "ddl_too_large",
                "submit",
                "DDL 超过配置的字节限制",
                http_status=422,
            )
        # 步骤二：生成不透明任务标识，并由原子脚本同时建立权威 Hash、来源租约
        # 与 dispatch outbox；脚本还保证同一逻辑来源只能有一个活动任务。
        job_id = str(uuid4())
        accepted = await self._state.submit(job_id, request, datetime.now(UTC))
        if not accepted:
            raise DataAgentError(
                "source_busy",
                "submit",
                "该逻辑数据源已有活动任务",
                http_status=409,
            )
        # 步骤三：从权威 Hash 回读公开投影，确保返回值不携带原始 DDL 或内部字段。
        record = await self.get(job_id)
        # 步骤四：权威状态成立后再尽力发布排队通知，发布失败不撤销受理结果。
        await self._publish_safely(
            record,
            JobEventType.PROGRESS,
            JobEventStage.QUEUED,
        )
        # 步骤五：受理成立后尽力立即调度，避免交互流程白等一个 cron 周期。
        await self._activate_now_safely(record.job_id, record.revision)
        # 步骤六：返回已持久化的公开记录，由 API 层据此报告 HTTP 202。
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
        # 步骤一：先读取权威公开投影；读取失败只记录安全告警，不影响已完成业务。
        try:
            record = await self.get(job_id)
        except RedisError:
            _log_event_publish_failure(job_id)
            return
        # 步骤二：使用权威投影构造通知，避免把 worker 内部载荷写入公开 Stream。
        await self._publish_safely(record, JobEventType.PROGRESS, stage)

    async def execution_input(self, job_id: str) -> DDLJobRequest:
        """读取仅供 worker 使用的初始请求。"""
        return await self._state.execution_input(job_id)

    async def stored_answers(self, job_id: str) -> str | None:
        """读取当前修订的内部回答 JSON。"""
        return await self._state.stored_answers(job_id)

    async def graph_version(self, job_id: str) -> str | None:
        """读取仅供 worker 使用的图版本兼容字段。"""
        return await self._state.graph_version(job_id)

    async def transition(
        self,
        job_id: str,
        revision: int,
        expected: JobStatus,
        target: JobStatus,
        *,
        fields: Mapping[str, str] | None = None,
        increment_attempt: bool = False,
    ) -> bool:
        """按集中状态表执行修订感知的原子转换。"""
        # 步骤一：先用集中状态表拒绝非法边，避免不同调用方各自解释生命周期。
        if target not in _ALLOWED_TRANSITIONS[expected]:
            raise ValueError(f"非法任务状态转换: {expected}->{target}")
        # 步骤二：由 Redis 脚本以 revision 作为 CAS 门闩执行转换，旧 worker
        # 因而不能覆盖更新或终态的权威记录。
        changed = await self._state.transition(
            job_id,
            revision,
            expected,
            target,
            fields=fields,
            increment_attempt=increment_attempt,
        )
        # 步骤三：只有原子转换胜出的调用方才回读并尽力发布新的公开投影。
        if changed and target in _STATUS_EVENT_TYPES:
            await self._publish_current_safely(
                job_id,
                _STATUS_EVENT_TYPES[target],
                _stage_for_status(target),
            )
        # 步骤四：把 CAS 是否胜出返回给 worker，由上层决定继续执行或退出。
        return changed

    async def mark_running(self, job_id: str, revision: int) -> bool:
        """Pending -> running，并原子增加尝试次数。"""
        # 步骤一：尝试次数由转换脚本内的 HINCRBY 完成，不再先读旧值再写回——
        # 读改写之间若有其他推进者插入，计数会被覆盖为陈旧值。
        return await self.transition(
            job_id,
            revision,
            JobStatus.PENDING,
            JobStatus.RUNNING,
            increment_attempt=True,
        )

    async def mark_waiting(
        self,
        job_id: str,
        revision: int,
        questions: list[MetricQuestion],
        question_round: int,
    ) -> bool:
        """Running -> waiting_input，并登记显式截止时间。"""
        # 步骤一：按统一等待时长计算绝对截止时间，供 API 与过期扫描共享。
        expires_at = datetime.now(UTC) + timedelta(
            seconds=app_config.redis.waiting_timeout_seconds
        )
        # 步骤二：原子保存问题集、轮次和截止时间后进入 waiting_input。
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
        # 步骤一：读取当前权威问题轮次，作为业务校验和 Lua CAS 的输入。
        record = await self.get(job_id)
        # 步骤二：在等待态先拒绝重复或未知问题 ID，避免无效载荷进入原子协议。
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
                raise DataAgentError(
                    "invalid_answers",
                    "waiting_input",
                    "回答必须唯一且只能引用当前问题",
                    http_status=422,
                    details={
                        "duplicate_ids": ",".join(sorted(duplicates)),
                        "unknown_ids": ",".join(sorted(unknown)),
                    },
                )
        # 步骤三：生成规范回答载荷及摘要，再由脚本原子裁决 revision、
        # question_set_id、截止时间和载荷哈希，使重复提交幂等且过期轮次不能恢复图。
        answer_json, answer_hash = JobCodec.answers_payload(request.answers)
        result = await self._state.submit_answers(
            job_id,
            record.source,
            revision=request.revision,
            question_set_id=request.question_set_id,
            answer_json=answer_json,
            answer_hash=answer_hash,
        )
        # 步骤四：把 Lua 返回码投影为稳定业务错误，保留超时与陈旧回答的差异。
        if result == -1:
            raise DataAgentError(
                "answer_timeout",
                "waiting_input",
                "回答已超过截止时间",
                http_status=410,
            )
        if result == 0:
            raise DataAgentError(
                "stale_answer",
                "waiting_input",
                "回答修订或问题集合已过期",
                http_status=409,
            )
        # 步骤五：回读脚本更新后的权威投影，仅对首次受理尽力发布排队通知，
        # 并立即调度新修订的激活，使人工回答后不再白等一个 cron 周期。
        updated = await self.get(job_id)
        if result == 1:
            await self._publish_safely(
                updated,
                JobEventType.PROGRESS,
                JobEventStage.QUEUED,
            )
            await self._activate_now_safely(job_id, updated.revision)
        # 步骤六：返回最新投影及首次受理标志，让 API 区分幂等重放。
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
        # 步骤一：只把安全结果或错误投影编码进终态字段，不携带内部异常文本。
        fields: dict[str, str] = {}
        if result is not None:
            fields["result_json"] = result.model_dump_json()
        if error is not None:
            fields["error_json"] = error.model_dump_json()
        # 步骤二：由原子转换同时写终态、释放来源租约、设置结果保留期并登记
        # checkpoint 清理 outbox；线程删除交给可重放维护任务完成。
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
        # 步骤一：按当前时间读取所有已越过截止时间的修订感知等待成员。
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
        # 步骤二：逐项执行 CAS 终态转换，只有仍处于对应等待修订的任务会被拒绝。
        # 每个成员独立隔离异常，避免单个孤儿成员或瞬时故障阻断同轮其余到期任务。
        expired: list[str] = []
        for member in members:
            job_id, revision_text = member.rsplit(":", 1)
            try:
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
            except DataAgentError as error:
                # 步骤三：权威 Hash 已过保留期的成员无法再转换，直接摘除孤儿成员，
                # 否则它会永远排在到期队列前部并重复失败。
                if error.code == "job_not_found":
                    await RedisBaseStore.awaitable(
                        self._redis.zrem(self.waiting_key, member)
                    )
                    _log_waiting_member_dropped(job_id)
                    continue
                raise
            except RedisError:
                # 步骤四：瞬时 Redis 故障只跳过当前成员，下个周期继续重放。
                _log_waiting_expire_deferred(job_id)
                continue
            if changed:
                expired.append(job_id)
        # 步骤五：只返回实际完成转换的任务，供维护任务协调后续清理。
        return expired

    async def heartbeat(self, job_id: str) -> None:
        """刷新任务在活动索引中的推进时间。

        每次 arq 激活开始时调用。任务在整个执行期间停留在 RUNNING、不产生状态
        转换，因此活动索引的分数只在激活边界推进；arq 自动重试同一激活时若不刷新，
        停滞判定会从第一次执行开始计时而误伤刚开始的重试。

        推进时间取 Redis 服务端时钟，与活动索引分数的写入时钟保持一致。
        """
        await self._activity.touch(job_id, await self._activity.now())

    async def reap_stalled(self, limit: int = 100) -> list[str]:
        """回收被基础设施重试预算耗尽后遗留的停滞任务。

        arq 的 `Retry` 与业务重试共用同一预算，预算耗尽后 arq 会静默放弃任务，
        此时权威记录仍停留在 pending 或 running，dispatch outbox 已被清空，没有
        任何组件会再次激活它。本方法按活动索引发现这类任务并重新激活或判定失败。

        Returns:
            实际被重新激活或判定失败的任务标识。
        """
        # 步骤一：停滞阈值必须严格大于 arq 自身的执行超时，确保被回退的 running
        # 任务对应的 arq 执行已被超时取消，不会与新激活并发执行。基准取 Redis 服务端
        # 时钟：活动索引分数由 Lua 用 TIME 写入，若这里改用 worker 主机时钟，两者偏移
        # 超过宽限期时仍在执行的任务会被回退成 pending，其终态转换随后 CAS 失败，而
        # 确定性 arq ID 又让重新入队被去重，长任务可能反复陷入该循环。
        threshold = await self._activity.now() - (
            app_config.redis.worker_job_timeout_seconds
            + app_config.redis.job_stall_grace_seconds
        )
        candidates = await self._activity.stalled(threshold, limit)
        # 步骤二：逐项独立隔离异常，单个候选失败不影响同轮其余停滞任务。
        reaped: list[str] = []
        for job_id in candidates:
            try:
                recovered = await self._reap_one(job_id)
            except RedisError:
                _log_stalled_reap_deferred(job_id)
                continue
            if recovered:
                reaped.append(job_id)
        # 步骤三：只返回真正推进了状态的任务，供维护任务观测回收结果。
        return reaped

    async def _reap_one(self, job_id: str) -> bool:
        """按权威记录裁决并执行单个停滞候选任务的恢复动作。"""
        # 步骤一：读取权威记录；已过保留期的成员无法恢复，直接摘除孤儿索引项。
        try:
            record = await self.get(job_id)
        except DataAgentError as error:
            if error.code == "job_not_found":
                await self._activity.drop(job_id)
                _log_stalled_member_dropped(job_id)
                return False
            raise
        # 步骤二：由纯裁决函数决定动作，恢复策略因此可脱离 Redis 单独验证。
        action = stall_action(
            record.status,
            record.attempt,
            app_config.redis.max_job_attempts,
        )
        if action is StallAction.DROP:
            await self._activity.drop(job_id)
            return False
        if action is StallAction.SKIP:
            await self._activity.touch(job_id, await self._activity.now())
            return False
        # 步骤三：超过累计激活上限的执行中任务进入终态，不再重新激活。
        if action is StallAction.FAIL:
            failed = await self.mark_terminal(
                job_id,
                record.revision,
                JobStatus.FAILED,
                error=JobError(
                    code="job_stalled",
                    stage="worker",
                    retryable=False,
                    attempt=record.attempt,
                ),
            )
            if failed:
                _log_stalled_job_failed(job_id)
            return failed
        # 步骤四：执行中任务先原子回退为待执行，CAS 失败说明已有其他推进者。
        if action is StallAction.RESET and not await self.transition(
            job_id,
            record.revision,
            JobStatus.RUNNING,
            JobStatus.PENDING,
        ):
            return False
        # 步骤五：重新登记激活请求；确定性 arq ID 保证对仍在队列的任务幂等。
        now = await self._activity.now()
        await self._outbox.enqueue_activation(job_id, record.revision, now)
        await self._activity.touch(job_id, now)
        _log_stalled_job_reactivated(job_id)
        return True

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
        # 步骤一：转换成功后重新读取权威投影，避免发布调用方持有的陈旧状态。
        try:
            record = await self.get(job_id)
        except RedisError:
            _log_event_publish_failure(job_id)
            return
        # 步骤二：把通知作为非阻断副作用发布，失败时保留已经提交的状态转换。
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
        except RedisError:
            _log_event_publish_failure(record.job_id)


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


def _log_event_publish_failure(job_id: str) -> None:
    """记录不包含任务输入和异常文本的安全事件写入告警。"""
    del job_id
    logger.warning("DDL 任务公开事件写入失败，客户端可通过权威快照恢复状态")


def _log_waiting_member_dropped(job_id: str) -> None:
    """记录已摘除的过期等待孤儿成员。"""
    del job_id
    logger.warning("等待成员对应的任务记录已过保留期，已摘除孤儿成员")


def _log_waiting_expire_deferred(job_id: str) -> None:
    """记录一个等待成员将在后续周期继续过期处理。"""
    del job_id
    logger.warning("等待任务过期处理失败，该成员保留并将在下个周期重试")


def _log_stalled_reap_deferred(job_id: str) -> None:
    """记录一个停滞任务将在后续周期继续回收。"""
    del job_id
    logger.warning("停滞任务回收失败，该任务保留在活动索引并将在下个周期重试")


def _log_stalled_member_dropped(job_id: str) -> None:
    """记录已摘除的活动索引孤儿成员。"""
    del job_id
    logger.warning("活动索引成员对应的任务记录已过保留期，已摘除孤儿成员")


def _log_stalled_job_reactivated(job_id: str) -> None:
    """记录一个已重新登记激活请求的停滞任务。"""
    del job_id
    logger.warning("检测到停滞任务，已重新登记激活请求")


def _log_stalled_job_failed(job_id: str) -> None:
    """记录一个超过激活上限而被判定失败的停滞任务。"""
    del job_id
    logger.warning("停滞任务已超过累计激活上限，已判定为失败终态")


def _log_immediate_dispatch_deferred(job_id: str) -> None:
    """记录一次退回周期调度的立即激活尝试。"""
    del job_id
    logger.warning("立即调度激活失败，调度请求保留在 outbox 并由周期任务重放")
