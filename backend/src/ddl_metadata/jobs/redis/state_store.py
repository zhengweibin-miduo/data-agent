"""DDL 任务 Hash 与多键原子状态持久化。"""

from collections.abc import Mapping
from datetime import UTC, datetime
from hashlib import sha256
from typing import cast

from redis.asyncio import Redis

from ddl_metadata.jobs.redis.base import RedisBaseStore
from ddl_metadata.jobs.redis.codec import JobCodec
from ddl_metadata.jobs.redis.keys import JobKeys
from ddl_metadata.jobs.redis.scripts import JobScripts
from errors import DataAgentError
from models.jobs import (
    DDLJobRequest,
    JobError,
    JobRecord,
    JobStatus,
)
from settings import app_config


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
    ) -> int:
        """原子写入来源租约、任务 Hash 和 dispatch outbox。"""
        # 步骤一：把来源租约、权威 Hash 与 dispatch outbox 交给同一 Lua 脚本提交，
        # 任一键无法建立时都不留下部分受理状态。
        accepted = await RedisBaseStore.awaitable(
            self._redis.eval(
                JobScripts.SUBMIT,
                4,
                self._keys.job(job_id),
                self._keys.dispatch,
                self._keys.source(request.source),
                self._keys.active,
                job_id,
                str(app_config.memory.source_lease_seconds),
                request.source,
                now.isoformat(),
                app_config.llm.graph_version,
                request.ddl,
                sha256(
                    f"{request.source}\0{request.dialect}\0{request.ddl}".encode()
                ).hexdigest(),
                str(now.timestamp()),
            )
        )
        # 步骤二：保留新建、幂等重放和冲突结果，供应用门面准确投影。
        return int(accepted)

    async def get(self, job_id: str) -> JobRecord:
        """读取公开安全任务投影。"""
        # 步骤一：读取任务权威 Hash，保留 Redis 对任务保留期的真实判断。
        values = await RedisBaseStore.awaitable(
            self._redis.hgetall(self._keys.job(job_id))
        )
        # 步骤二：空 Hash 统一投影为安全的任务不存在错误。
        if not values:
            raise DataAgentError(
                "job_not_found",
                "job_status",
                "任务不存在或已过保留期",
                http_status=404,
            )
        # 步骤三：通过集中 codec 丢弃原始 DDL 和内部回答后返回公开契约。
        return JobCodec.project(cast(Mapping[str, str], values))

    async def execution_input(self, job_id: str) -> DDLJobRequest:
        """读取仅供 worker 使用的初始请求。"""
        # 步骤一：只读取 worker 首次执行所需的来源、方言与原始 DDL 字段。
        values = await RedisBaseStore.awaitable(
            self._redis.hmget(
                self._keys.job(job_id),
                ["source", "dialect", "ddl"],
            )
        )
        # 步骤二：缺少任一内部字段都视为不可执行任务，不构造残缺输入。
        if not all(values):
            raise DataAgentError(
                "job_not_found",
                "worker",
                "任务执行输入不存在",
                http_status=404,
            )
        # 步骤三：恢复类型化请求，仅在内部 worker 边界暴露原始 DDL。
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

    async def graph_version(self, job_id: str) -> str | None:
        """读取仅供 worker 使用的图版本兼容字段。

        该字段只用于判断任务能否由当前图版本继续解释，对调用方没有任何用途，
        因此不进入公开投影，改由 worker 走内部读取路径获取。
        """
        return await RedisBaseStore.awaitable(
            self._redis.hget(self._keys.job(job_id), "graph_version")
        )

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
        """执行修订感知的原子状态转换。

        Args:
            job_id: 任务标识。
            revision: 期望的当前修订号，作为 CAS 门闩。
            expected: 期望的当前状态。
            target: 目标状态。
            fields: 随转换一并写入的扩展字段。
            increment_attempt: 是否在同一脚本内原子递增尝试次数。调用方不再
                自行读取旧值再写回，避免读改写之间被其他推进者插入。

        Returns:
            原子转换是否胜出。
        """
        # 步骤一：先读取权威记录以确定来源租约键，避免调用方传入第二份来源。
        record = await self.get(job_id)
        # 步骤二：统一计算时间、等待成员和扩展字段，形成稳定 Lua 参数协议。
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
        arguments.append(str(int(increment_attempt)))
        # 步骤三：由 Lua 以状态和 revision 做 CAS，并在需要时原子维护等待集合、
        # 来源租约、结果 TTL 与 checkpoint cleanup outbox。
        changed = await RedisBaseStore.awaitable(
            self._redis.eval(
                JobScripts.TRANSITION,
                5,
                self._keys.job(job_id),
                self._keys.waiting,
                self._keys.source(record.source),
                self._keys.checkpoint_cleanup,
                self._keys.active,
                *arguments,
            )
        )
        # 步骤四：只把原子转换是否胜出返回给应用门面。
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
        # 步骤一：预先派生下一修订和统一时间坐标，确保所有 Redis 键使用同一轮次。
        next_revision = revision + 1
        now = datetime.now(UTC)
        # 步骤二：由 Lua 一次性校验状态、修订、问题集、截止时间及回答摘要，
        # 并原子更新任务、等待集合、来源租约、dispatch 与 cleanup outbox。
        return int(
            await RedisBaseStore.awaitable(
                self._redis.eval(
                    JobScripts.ANSWER,
                    6,
                    self._keys.job(job_id),
                    self._keys.waiting,
                    self._keys.dispatch,
                    self._keys.source(source),
                    self._keys.checkpoint_cleanup,
                    self._keys.active,
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
