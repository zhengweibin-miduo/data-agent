"""停滞任务裁决规则与回收编排测试。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from data_agent.ddl_metadata.jobs.recovery import StallAction, stall_action
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.errors import DataAgentError
from data_agent.models.jobs import JobRecord, JobStatus
from data_agent.settings import app_config
from tests.helpers.checks import check_equal


def test_stall_action_covers_every_status() -> None:
    """锁定停滞裁决对每个状态与尝试预算的结果。"""
    cases = (
        ("终态成功", JobStatus.SUCCEEDED, 0, StallAction.DROP),
        ("终态拒绝", JobStatus.REJECTED, 0, StallAction.DROP),
        ("终态失败", JobStatus.FAILED, 0, StallAction.DROP),
        ("等待人工回答", JobStatus.WAITING_INPUT, 0, StallAction.SKIP),
        ("已受理未执行", JobStatus.PENDING, 0, StallAction.REACTIVATE),
        ("执行中且预算充足", JobStatus.RUNNING, 1, StallAction.RESET),
        ("执行中且预算耗尽", JobStatus.RUNNING, 3, StallAction.FAIL),
        ("执行中且超出预算", JobStatus.RUNNING, 4, StallAction.FAIL),
    )
    for label, status, attempt, expected in cases:
        check_equal(label, stall_action(status, attempt, 3), expected)


def _record(
    job_id: str,
    status: JobStatus,
    *,
    revision: int = 0,
    attempt: int = 0,
) -> JobRecord:
    """构造停滞候选的权威投影。"""
    moment = datetime.now(UTC)
    return JobRecord(
        job_id=job_id,
        source="dw",
        status=status,
        revision=revision,
        attempt=attempt,
        created_at=moment,
        updated_at=moment,
        graph_version=app_config.llm.graph_version,
    )


class _FakeStateStore:
    """按预置投影响应读取并记录状态转换。"""

    def __init__(self, records: dict[str, JobRecord]) -> None:
        """绑定预置的权威投影集合。"""
        self._records = records
        self.transitions: list[tuple[str, JobStatus, JobStatus, dict[str, str]]] = []

    async def get(self, job_id: str) -> JobRecord:
        """返回预置投影，缺失时抛出稳定的任务不存在错误。"""
        record = self._records.get(job_id)
        if record is None:
            raise DataAgentError(
                "job_not_found",
                "job_status",
                "任务不存在或已过保留期",
                http_status=404,
            )
        return record

    async def transition(
        self,
        job_id: str,
        revision: int,
        expected: JobStatus,
        target: JobStatus,
        *,
        fields: Mapping[str, str] | None = None,
    ) -> bool:
        """记录转换请求并统一报告 CAS 胜出。"""
        self.transitions.append((job_id, expected, target, dict(fields or {})))
        self._records[job_id] = _record(
            job_id,
            target,
            revision=revision,
            attempt=self._records[job_id].attempt,
        )
        return True


class _FakeActivityStore:
    """记录活动索引的读取、刷新与摘除。"""

    def __init__(self, candidates: list[str]) -> None:
        """绑定预置的停滞候选。"""
        self._candidates = candidates
        self.touched: list[str] = []
        self.dropped: list[str] = []

    async def stalled(self, threshold: float, limit: int) -> list[str]:
        """返回预置候选，忽略阈值以保持测试确定性。"""
        return self._candidates

    async def touch(self, job_id: str, at: float) -> None:
        """记录一次推进时间刷新。"""
        self.touched.append(job_id)

    async def drop(self, job_id: str) -> None:
        """记录一次索引成员摘除。"""
        self.dropped.append(job_id)


class _FakeOutboxStore:
    """记录重新登记的激活请求。"""

    def __init__(self) -> None:
        """初始化激活轨迹。"""
        self.activations: list[tuple[str, int]] = []

    async def enqueue_activation(
        self,
        job_id: str,
        revision: int,
        at: float,
    ) -> None:
        """记录一次激活登记。"""
        self.activations.append((job_id, revision))


class _FakeEventStore:
    """吞掉公开事件发布，保持回收路径可单测。"""

    async def publish(self, job_id: str, event_type: object, data: object) -> None:
        """忽略事件发布。"""


class _BrokenActivityStore(_FakeActivityStore):
    """对指定任务的刷新抛出瞬时 Redis 故障。"""

    def __init__(self, candidates: list[str], failing: str) -> None:
        """绑定候选与需要制造故障的任务。"""
        super().__init__(candidates)
        self._failing = failing

    async def touch(self, job_id: str, at: float) -> None:
        """对目标任务抛出瞬时故障，其余照常记录。"""
        if job_id == self._failing:
            raise RedisError("connection reset")
        await super().touch(job_id, at)


def _store(
    records: dict[str, JobRecord],
    candidates: list[str],
    *,
    activity: _FakeActivityStore | None = None,
) -> tuple[DDLJobStore, _FakeStateStore, _FakeActivityStore, _FakeOutboxStore]:
    """构造注入记录型替身的任务门面。"""
    store = DDLJobStore(cast(Redis, object()))
    state = _FakeStateStore(records)
    activity_store = activity or _FakeActivityStore(candidates)
    outbox = _FakeOutboxStore()
    store._state = cast(object, state)  # type: ignore[assignment]
    store._activity = cast(object, activity_store)  # type: ignore[assignment]
    store._outbox = cast(object, outbox)  # type: ignore[assignment]
    store._events = cast(object, _FakeEventStore())  # type: ignore[assignment]
    return store, state, activity_store, outbox


async def test_reap_stalled_reactivates_pending_job() -> None:
    """已受理未执行的停滞任务只需重新登记激活请求。"""
    records = {"job-1": _record("job-1", JobStatus.PENDING, revision=2)}
    store, state, activity, outbox = _store(records, ["job-1"])

    reaped = await store.reap_stalled()

    check_equal("回收结果", reaped, ["job-1"])
    check_equal("重新登记的激活请求", outbox.activations, [("job-1", 2)])
    check_equal("pending 任务不做状态转换", state.transitions, [])
    check_equal("刷新推进时间避免重复巡检", activity.touched, ["job-1"])
    check_equal("pending 任务不摘除索引", activity.dropped, [])


async def test_reap_stalled_resets_running_job_within_budget() -> None:
    """预算充足的执行中停滞任务先回退再重新激活。"""
    records = {"job-2": _record("job-2", JobStatus.RUNNING, revision=1, attempt=1)}
    store, state, _, outbox = _store(records, ["job-2"])

    reaped = await store.reap_stalled()

    check_equal("回收结果", reaped, ["job-2"])
    check_equal(
        "回退为待执行后再登记激活",
        [(item[1], item[2]) for item in state.transitions],
        [(JobStatus.RUNNING, JobStatus.PENDING)],
    )
    check_equal("重新登记的激活请求", outbox.activations, [("job-2", 1)])


async def test_reap_stalled_fails_running_job_beyond_budget() -> None:
    """超过累计激活上限的执行中任务进入失败终态。"""
    attempts = app_config.redis.max_job_attempts
    records = {
        "job-3": _record("job-3", JobStatus.RUNNING, revision=0, attempt=attempts)
    }
    store, state, _, outbox = _store(records, ["job-3"])

    reaped = await store.reap_stalled()

    check_equal("回收结果", reaped, ["job-3"])
    check_equal(
        "转入失败终态",
        [(item[1], item[2]) for item in state.transitions],
        [(JobStatus.RUNNING, JobStatus.FAILED)],
    )
    check_equal(
        "携带稳定错误码",
        json.loads(state.transitions[0][3]["error_json"])["code"],
        "job_stalled",
    )
    check_equal("失败任务不再重新激活", outbox.activations, [])


async def test_reap_stalled_skips_waiting_and_drops_terminal() -> None:
    """等待人工回答仅刷新，终态与孤儿成员被摘除。"""
    records = {
        "job-4": _record("job-4", JobStatus.WAITING_INPUT),
        "job-5": _record("job-5", JobStatus.SUCCEEDED),
    }
    store, state, activity, outbox = _store(
        records,
        ["job-4", "job-5", "job-missing"],
    )

    reaped = await store.reap_stalled()

    check_equal("三类候选均不计入回收结果", reaped, [])
    check_equal("等待任务只刷新推进时间", activity.touched, ["job-4"])
    check_equal(
        "终态与孤儿成员被摘除",
        activity.dropped,
        ["job-5", "job-missing"],
    )
    check_equal("三类候选均不重新激活", outbox.activations, [])
    check_equal("三类候选均不做状态转换", state.transitions, [])


async def test_reap_stalled_isolates_single_candidate_failure() -> None:
    """单个候选的瞬时故障不影响同轮其余停滞任务。"""
    records = {
        "job-6": _record("job-6", JobStatus.WAITING_INPUT),
        "job-7": _record("job-7", JobStatus.PENDING, revision=3),
    }
    store, _, _, outbox = _store(
        records,
        ["job-6", "job-7"],
        activity=_BrokenActivityStore(["job-6", "job-7"], "job-6"),
    )

    reaped = await store.reap_stalled()

    check_equal("故障候选之后的任务仍被回收", reaped, ["job-7"])
    check_equal("后续候选完成激活登记", outbox.activations, [("job-7", 3)])
