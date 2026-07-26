"""DDL 任务 Hash 与多键原子状态持久化。"""

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from redis.asyncio import Redis

from data_agent.ddl_metadata.jobs.redis.base import RedisBaseStore
from data_agent.ddl_metadata.jobs.redis.codec import JobCodec
from data_agent.ddl_metadata.jobs.redis.keys import JobKeys
from data_agent.ddl_metadata.jobs.redis.scripts import JobScripts
from data_agent.errors import DataAgentError
from data_agent.models.jobs import (
    DDLJobRequest,
    JobError,
    JobRecord,
    JobStatus,
)
from data_agent.settings import app_config


class RedisJobStateStore:
    """拥有任务记录读取及提交、转换、回答的原子 Redis 协议。"""

    def __init__(self, redis: Redis, keys: JobKeys) -> None:
        """绑定 Redis 客户端与任务键空间。"""
        self._redis = redis
        self._keys = keys

    async def submit(
        self,
        job_id: str,
        request: DDLJobRequest,
        now: datetime,
    ) -> bool:
        """原子写入来源租约、任务 Hash 和 dispatch outbox。"""
        accepted = await RedisBaseStore.awaitable(
            self._redis.eval(
                JobScripts.SUBMIT,
                3,
                self._keys.job(job_id),
                self._keys.dispatch,
                self._keys.source(request.source),
                job_id,
                str(app_config.memory.source_lease_seconds),
                request.source,
                now.isoformat(),
                app_config.llm.graph_version,
                request.ddl,
                str(now.timestamp()),
            )
        )
        return int(accepted) == 1

    async def get(self, job_id: str) -> JobRecord:
        """读取公开安全任务投影。"""
        values = await RedisBaseStore.awaitable(
            self._redis.hgetall(self._keys.job(job_id))
        )
        if not values:
            raise DataAgentError(
                "job_not_found",
                "job_status",
                "任务不存在或已过保留期",
                http_status=404,
            )
        return JobCodec.project(cast(Mapping[str, str], values))

    async def execution_input(self, job_id: str) -> DDLJobRequest:
        """读取仅供 worker 使用的初始请求。"""
        values = await RedisBaseStore.awaitable(
            self._redis.hmget(
                self._keys.job(job_id),
                ["source", "dialect", "ddl"],
            )
        )
        if not all(values):
            raise DataAgentError(
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
        return await RedisBaseStore.awaitable(
            self._redis.hget(self._keys.job(job_id), "answer_json")
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
        """执行修订感知的原子状态转换。"""
        record = await self.get(job_id)
        now = datetime.now(UTC)
        values = dict(fields or {})
        expires_epoch = float(values.get("expires_at_epoch", "0"))
        member = self._keys.activation_member(job_id, revision)
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
        changed = await RedisBaseStore.awaitable(
            self._redis.eval(
                JobScripts.TRANSITION,
                4,
                self._keys.job(job_id),
                self._keys.waiting,
                self._keys.source(record.source),
                self._keys.checkpoint_cleanup,
                *arguments,
            )
        )
        return int(changed) == 1

    async def submit_answers(
        self,
        job_id: str,
        source: str,
        *,
        revision: int,
        question_set_id: str,
        answer_json: str,
        answer_hash: str,
    ) -> int:
        """原子验证回答并安排下一修订。

        Returns:
            Lua 协议返回码：首次受理为 1，幂等重复为 2，过期为 -1，
            状态或修订不匹配为 0。
        """
        next_revision = revision + 1
        now = datetime.now(UTC)
        return int(
            await RedisBaseStore.awaitable(
                self._redis.eval(
                    JobScripts.ANSWER,
                    5,
                    self._keys.job(job_id),
                    self._keys.waiting,
                    self._keys.dispatch,
                    self._keys.source(source),
                    self._keys.checkpoint_cleanup,
                    str(revision),
                    question_set_id,
                    str(now.timestamp()),
                    answer_hash,
                    str(next_revision),
                    now.isoformat(),
                    answer_json,
                    self._keys.activation_member(job_id, revision),
                    self._keys.activation_member(job_id, next_revision),
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


_TERMINAL = {JobStatus.SUCCEEDED, JobStatus.REJECTED, JobStatus.FAILED}
