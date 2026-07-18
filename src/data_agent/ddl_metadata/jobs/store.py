"""Redis 公开任务投影、修订状态机与 dispatch outbox。"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import ClassVar, TypeVar, cast
from uuid import uuid4

from arq.connections import ArqRedis
from redis.asyncio import Redis

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.models import (
    AnswerRequest,
    DDLJobRequest,
    JobError,
    JobRecord,
    JobResult,
    JobStatus,
    MetricQuestion,
)
from data_agent.settings import app_config

_SUBMIT_SCRIPT = """
if redis.call('SET', KEYS[3], ARGV[1], 'EX', ARGV[2], 'NX') == false then
  return 0
end
redis.call('HSET', KEYS[1],
  'job_id', ARGV[1], 'source', ARGV[3], 'status', 'pending',
  'revision', '0', 'attempt', '0', 'question_round', '0',
  'created_at', ARGV[4], 'updated_at', ARGV[4],
  'graph_version', ARGV[5], 'ddl', ARGV[6], 'dialect', 'mysql')
redis.call('ZADD', KEYS[2], ARGV[7], ARGV[1] .. ':0')
return 1
"""

_TRANSITION_SCRIPT = """
if redis.call('HGET', KEYS[1], 'status') ~= ARGV[1] then return 0 end
if redis.call('HGET', KEYS[1], 'revision') ~= ARGV[3] then return 0 end
redis.call('HSET', KEYS[1], 'status', ARGV[2], 'updated_at', ARGV[4])
local field_count = tonumber(ARGV[8])
for index = 0, field_count - 1 do
  redis.call('HSET', KEYS[1], ARGV[9 + index * 2], ARGV[10 + index * 2])
end
redis.call('ZREM', KEYS[2], ARGV[5])
if ARGV[2] == 'waiting_input' then
  redis.call('ZADD', KEYS[2], ARGV[6], ARGV[5])
end
if ARGV[7] == '1' then
  if redis.call('GET', KEYS[3]) == redis.call('HGET', KEYS[1], 'job_id') then
    redis.call('DEL', KEYS[3])
  end
  local redis_time = redis.call('TIME')
  redis.call('ZADD', KEYS[4], redis_time[1],
    redis.call('HGET', KEYS[1], 'job_id'))
  redis.call('HDEL', KEYS[1],
    'ddl', 'answer_json', 'answer_hash', 'questions_json',
    'question_set_id', 'expires_at', 'expires_at_epoch')
  redis.call('EXPIRE', KEYS[1], ARGV[8 + field_count * 2 + 1])
end
return 1
"""

_ANSWER_SCRIPT = """
local status = redis.call('HGET', KEYS[1], 'status')
local revision = redis.call('HGET', KEYS[1], 'revision')
if status ~= 'waiting_input' then
  if redis.call('HGET', KEYS[1], 'answer_hash') == ARGV[4]
     and redis.call('HGET', KEYS[1], 'question_set_id') == ARGV[2]
     and revision == ARGV[5] then return 2 end
  return 0
end
if revision ~= ARGV[1]
   or redis.call('HGET', KEYS[1], 'question_set_id') ~= ARGV[2] then
  return 0
end
if tonumber(redis.call('HGET', KEYS[1], 'expires_at_epoch')) <= tonumber(ARGV[3]) then
  redis.call('HSET', KEYS[1], 'status', 'rejected', 'updated_at', ARGV[6],
    'error_json', ARGV[10])
  redis.call('ZREM', KEYS[2], ARGV[8])
  if redis.call('GET', KEYS[4]) == redis.call('HGET', KEYS[1], 'job_id') then
    redis.call('DEL', KEYS[4])
  end
  redis.call('ZADD', KEYS[5], ARGV[3], redis.call('HGET', KEYS[1], 'job_id'))
  redis.call('HDEL', KEYS[1],
    'ddl', 'answer_json', 'answer_hash', 'questions_json',
    'question_set_id', 'expires_at', 'expires_at_epoch')
  redis.call('EXPIRE', KEYS[1], ARGV[11])
  return -1
end
redis.call('HSET', KEYS[1],
  'status', 'pending', 'revision', ARGV[5], 'updated_at', ARGV[6],
  'answer_hash', ARGV[4], 'answer_json', ARGV[7])
redis.call('ZREM', KEYS[2], ARGV[8])
redis.call('ZADD', KEYS[3], ARGV[3], ARGV[9])
redis.call('EXPIRE', KEYS[4], ARGV[12])
return 1
"""

_RENEW_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

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
_TERMINAL = {JobStatus.SUCCEEDED, JobStatus.REJECTED, JobStatus.FAILED}
RedisResultT = TypeVar("RedisResultT")


def _redis_awaitable(
    value: Awaitable[RedisResultT] | RedisResultT,
) -> Awaitable[RedisResultT]:
    """收窄 redis-py 为同步/异步客户端共享的返回类型。"""
    return cast(Awaitable[RedisResultT], value)


def question_set_id(questions: list[MetricQuestion]) -> str:
    """生成问题集合的规范 SHA-256 标识。"""
    value = json.dumps(
        [question.model_dump(mode="json") for question in questions],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


class DDLJobStore:
    """集中拥有 Redis 任务状态与修订转换。"""

    _prefix: ClassVar[str] = app_config.redis.key_prefix

    def __init__(self, redis: Redis) -> None:
        """绑定已初始化的应用 Redis 客户端。"""
        self._redis = redis

    @property
    def dispatch_key(self) -> str:
        """返回 dispatch outbox 键。"""
        return f"{self._prefix}:dispatch"

    @property
    def waiting_key(self) -> str:
        """返回等待截止时间有序集合键。"""
        return f"{self._prefix}:waiting"

    @property
    def cleanup_key(self) -> str:
        """返回终态检查点清理 outbox 键。"""
        return f"{self._prefix}:checkpoint_cleanup"

    def _job_key(self, job_id: str) -> str:
        return f"{self._prefix}:job:{job_id}"

    def _source_key(self, source: str) -> str:
        return f"{self._prefix}:source:{source}"

    async def submit(self, request: DDLJobRequest) -> JobRecord:
        """原子写入任务和 outbox 后才报告受理。"""
        if len(request.ddl.encode()) > app_config.api.max_ddl_bytes:
            raise DDLMetadataError(
                "ddl_too_large",
                "submit",
                "DDL 超过配置的字节限制",
                http_status=422,
            )
        now = datetime.now(UTC)
        job_id = str(uuid4())
        accepted = await _redis_awaitable(
            self._redis.eval(
                _SUBMIT_SCRIPT,
                3,
                self._job_key(job_id),
                self.dispatch_key,
                self._source_key(request.source),
                job_id,
                str(app_config.memory.source_lease_seconds),
                request.source,
                now.isoformat(),
                app_config.llm.graph_version,
                request.ddl,
                str(now.timestamp()),
            )
        )
        if int(accepted) != 1:
            raise DDLMetadataError(
                "source_busy",
                "submit",
                "该逻辑数据源已有活动任务",
                http_status=409,
            )
        return await self.get(job_id)

    async def get(self, job_id: str) -> JobRecord:
        """读取公开安全任务投影。"""
        values = await _redis_awaitable(self._redis.hgetall(self._job_key(job_id)))
        if not values:
            raise DDLMetadataError(
                "job_not_found",
                "job_status",
                "任务不存在或已过保留期",
                http_status=404,
            )
        return self._project(cast(Mapping[str, str], values))

    def _project(self, values: Mapping[str, str]) -> JobRecord:
        """从内部 Hash 字段构造公开契约。"""
        return JobRecord(
            job_id=values["job_id"],
            source=values["source"],
            status=JobStatus(values["status"]),
            revision=int(values.get("revision", "0")),
            attempt=int(values.get("attempt", "0")),
            question_round=int(values.get("question_round", "0")),
            question_set_id=values.get("question_set_id") or None,
            questions=(
                [
                    MetricQuestion.model_validate(question)
                    for question in json.loads(values["questions_json"])
                ]
                if values.get("questions_json")
                else None
            ),
            result=(
                JobResult.model_validate_json(values["result_json"])
                if values.get("result_json")
                else None
            ),
            error=(
                JobError.model_validate_json(values["error_json"])
                if values.get("error_json")
                else None
            ),
            created_at=datetime.fromisoformat(values["created_at"]),
            updated_at=datetime.fromisoformat(values["updated_at"]),
            expires_at=(
                datetime.fromisoformat(values["expires_at"])
                if values.get("expires_at")
                else None
            ),
            graph_version=values["graph_version"],
        )

    async def execution_input(self, job_id: str) -> DDLJobRequest:
        """读取仅供 worker 使用的初始请求。"""
        values = await _redis_awaitable(
            self._redis.hmget(
                self._job_key(job_id),
                ["source", "dialect", "ddl"],
            )
        )
        if not all(values):
            raise DDLMetadataError(
                "job_not_found",
                "worker",
                "任务执行输入不存在",
                http_status=404,
            )
        return DDLJobRequest(
            source=values[0],
            dialect=values[1],
            ddl=values[2],
        )

    async def stored_answers(self, job_id: str) -> str | None:
        """读取当前修订的内部回答 JSON。"""
        return await _redis_awaitable(
            self._redis.hget(self._job_key(job_id), "answer_json")
        )

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
        record = await self.get(job_id)
        now = datetime.now(UTC)
        values = dict(fields or {})
        expires_epoch = float(values.get("expires_at_epoch", "0"))
        member = f"{job_id}:{revision}"
        arguments: list[str] = [
            expected.value,
            target.value,
            str(revision),
            now.isoformat(),
            member,
            str(expires_epoch),
            str(int(target in _TERMINAL)),
            str(len(values)),
        ]
        arguments.extend(item for pair in values.items() for item in pair)
        arguments.append(str(app_config.redis.result_retention_seconds))
        changed = await _redis_awaitable(
            self._redis.eval(
                _TRANSITION_SCRIPT,
                4,
                self._job_key(job_id),
                self.waiting_key,
                self._source_key(record.source),
                self.cleanup_key,
                *arguments,
            )
        )
        return int(changed) == 1

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
                "questions_json": json.dumps(
                    [question.model_dump(mode="json") for question in questions],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
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
                question_id
                for question_id in answer_ids
                if answer_ids.count(question_id) > 1
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
        answer_json = json.dumps(
            [answer.model_dump(mode="json") for answer in request.answers],
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        answer_hash = hashlib.sha256(answer_json.encode()).hexdigest()
        next_revision = request.revision + 1
        now = datetime.now(UTC)
        result = int(
            await _redis_awaitable(
                self._redis.eval(
                    _ANSWER_SCRIPT,
                    5,
                    self._job_key(job_id),
                    self.waiting_key,
                    self.dispatch_key,
                    self._source_key(record.source),
                    self.cleanup_key,
                    str(request.revision),
                    request.question_set_id,
                    str(now.timestamp()),
                    answer_hash,
                    str(next_revision),
                    now.isoformat(),
                    answer_json,
                    f"{job_id}:{request.revision}",
                    f"{job_id}:{next_revision}",
                    JobError(
                        code="answer_timeout",
                        stage="waiting_input",
                        retryable=False,
                    ).model_dump_json(),
                    str(app_config.redis.result_retention_seconds),
                    str(app_config.memory.source_lease_seconds),
                )
            )
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
        return await self.get(job_id), result == 1

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
        key = self._source_key(source)
        return bool(
            await _redis_awaitable(
                self._redis.eval(
                    _RENEW_SCRIPT,
                    1,
                    key,
                    job_id,
                    str(app_config.memory.source_lease_seconds),
                )
            )
        )

    @asynccontextmanager
    async def mutation_lease(self, source: str) -> AsyncIterator[None]:
        """短暂序列化浏览器记忆变更与活动图任务。"""
        token = f"mutation:{uuid4()}"
        key = self._source_key(source)
        acquired = await _redis_awaitable(
            self._redis.set(
                key,
                token,
                ex=min(app_config.memory.source_lease_seconds, 60),
                nx=True,
            )
        )
        if not acquired:
            raise DDLMetadataError(
                "source_busy",
                "memory_mutation",
                "该逻辑数据源有活动任务",
                http_status=409,
            )
        try:
            yield
        finally:
            await _redis_awaitable(self._redis.eval(_RELEASE_SCRIPT, 1, key, token))

    async def dispatch(self, queue: ArqRedis, limit: int = 100) -> int:
        """把 outbox 激活幂等写入 arq 后删除已处理项。"""
        members = cast(
            list[str],
            await _redis_awaitable(self._redis.zrange(self.dispatch_key, 0, limit - 1)),
        )
        dispatched = 0
        for member in members:
            job_id, revision_text = member.rsplit(":", 1)
            await queue.enqueue_job(
                "run_ddl_job",
                job_id,
                int(revision_text),
                _job_id=f"{self._prefix}:{job_id}:{revision_text}",
            )
            await _redis_awaitable(self._redis.zrem(self.dispatch_key, member))
            dispatched += 1
        return dispatched

    async def expire_waiting(self) -> list[str]:
        """拒绝已到期等待轮次；检查点清理由 worker cron 协调。"""
        now = time.time()
        members = cast(
            list[str],
            await _redis_awaitable(
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
        return cast(
            list[str],
            await _redis_awaitable(self._redis.zrange(self.cleanup_key, 0, limit - 1)),
        )

    async def acknowledge_checkpoint_cleanup(self, job_id: str) -> None:
        """仅在检查点删除成功后确认清理 outbox。"""
        await _redis_awaitable(self._redis.zrem(self.cleanup_key, job_id))
