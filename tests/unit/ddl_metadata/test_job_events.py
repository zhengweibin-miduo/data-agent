"""DDL 任务公开事件模型和 SSE 生成检查。"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

from fastapi import Request
from redis.exceptions import ConnectionError as RedisConnectionError

from data_agent.ddl_metadata.api.job_events import stream_job_events
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.models.jobs import (
    DDLJobAccepted,
    JobError,
    JobEvent,
    JobEventStage,
    JobEventType,
    JobRecord,
    JobResult,
    JobStatus,
)
from data_agent.models.semantic import MetricQuestion
from tests.helpers.checks import check_condition, check_equal


def _record(
    status: JobStatus,
    *,
    revision: int = 0,
) -> JobRecord:
    """构造不包含内部 DDL 的公开任务投影。"""
    question = MetricQuestion(
        question_id="q-1",
        prompt="营业额口径？",
        fact_table_id="fact-1",
        column_ids=["amount"],
    )
    return JobRecord(
        job_id="job-1",
        source="source-1",
        status=status,
        revision=revision,
        attempt=1,
        question_round=1,
        question_set_id="sha256:test",
        questions=[question] if status == JobStatus.WAITING_INPUT else None,
        result=(
            JobResult(
                ddl_hash="sha256:public",
                table_count=1,
                column_count=2,
                metric_count=1,
            )
            if status == JobStatus.SUCCEEDED
            else None
        ),
        error=(
            JobError(
                code="safe_failure",
                stage="worker",
                retryable=False,
                attempt=1,
            )
            if status in {JobStatus.REJECTED, JobStatus.FAILED}
            else None
        ),
        created_at=datetime(2026, 7, 19, tzinfo=UTC),
        updated_at=datetime(2026, 7, 19, 0, 1, tzinfo=UTC),
    )


class _ConnectedRequest:
    """模拟始终保持连接的请求。"""

    async def is_disconnected(self) -> bool:
        """报告客户端仍连接。"""
        return False


class _DisconnectedRequest:
    """模拟已断开的客户端。"""

    async def is_disconnected(self) -> bool:
        """报告客户端已经断开。"""
        return True


class _EventJobs:
    """为有界 SSE 生成器提供确定性事件批次。"""

    def __init__(
        self,
        batches: list[list[JobEvent] | Exception],
        snapshots: list[JobRecord] | None = None,
    ) -> None:
        """保存读取批次和权威快照。"""
        self.batches = batches
        self.snapshots = snapshots or []
        self.cursors: list[str] = []

    @staticmethod
    def snapshot_event(record: JobRecord, event_id: str) -> JobEvent:
        """复用应用门面的公开快照投影。"""
        return DDLJobStore.snapshot_event(record, event_id)

    async def read_events(
        self,
        job_id: str,
        after_id: str,
        *,
        block_milliseconds: int,
    ) -> list[JobEvent]:
        """返回下一批事件或抛出预设故障。"""
        del job_id, block_milliseconds
        self.cursors.append(after_id)
        batch = self.batches.pop(0)
        if isinstance(batch, Exception):
            raise batch
        return batch

    async def get(self, job_id: str) -> JobRecord:
        """返回下一份权威快照。"""
        del job_id
        return self.snapshots.pop(0)


async def _collect(stream: AsyncIterator[str]) -> list[str]:
    """完整消费会自然结束的测试事件流。"""
    return [frame async for frame in stream]


def _event(
    event_id: str,
    event_type: JobEventType,
    record: JobRecord,
    stage: JobEventStage,
) -> JobEvent:
    """构造安全的已存储公开事件。"""
    snapshot = DDLJobStore.snapshot_event(record, event_id)
    return snapshot.model_copy(
        update={
            "event_type": event_type,
            "data": snapshot.data.model_copy(update={"stage": stage}),
        }
    )


def test_job_event_contract_is_backward_compatible_and_safe() -> None:
    """受理响应保持兼容，事件数据不包含任务输入与内部文本。"""
    old_response = DDLJobAccepted(
        job_id="job-1",
        status_url="/api/v1/metadata/ddl-jobs/job-1",
    )
    check_equal("旧受理响应可省略 events_url", old_response.events_url, None)
    event = DDLJobStore.snapshot_event(_record(JobStatus.WAITING_INPUT), "1-0")
    payload = event.model_dump_json()
    check_condition(
        "事件包含公开问题",
        "营业额口径" in payload,
        actual=payload,
        expected="包含当前公开问题",
    )
    check_condition(
        "事件排除内部字段",
        all(
            token not in payload
            for token in ("CREATE TABLE", "model_prompt", "exception_text", '"source"')
        ),
        actual=payload,
        expected="不含 DDL、模型提示、内部异常和 source",
    )


async def test_terminal_snapshot_closes_stream() -> None:
    """终态新连接只发送权威快照并正常关闭。"""
    jobs = _EventJobs([])
    frames = await _collect(
        stream_job_events(
            cast(Request, _ConnectedRequest()),
            cast(DDLJobStore, jobs),
            _record(JobStatus.SUCCEEDED),
            "9-0",
        )
    )
    check_equal("终态帧数量", len(frames), 1)
    check_condition(
        "终态快照 SSE 格式",
        frames[0].startswith("id: 9-0\nevent: snapshot\ndata: {"),
        actual=frames[0],
        expected="标准 id/event/data 帧",
    )


async def test_waiting_stream_continues_into_next_revision_and_terminal() -> None:
    """等待回答不会关闭，同一连接继续接收下一修订和成功结果。"""
    queued = _event(
        "2-0",
        JobEventType.PROGRESS,
        _record(JobStatus.PENDING, revision=1),
        JobEventStage.QUEUED,
    )
    succeeded = _event(
        "3-0",
        JobEventType.SUCCEEDED,
        _record(JobStatus.SUCCEEDED, revision=1),
        JobEventStage.SUCCEEDED,
    )
    jobs = _EventJobs([[queued, succeeded]])
    frames = await _collect(
        stream_job_events(
            cast(Request, _ConnectedRequest()),
            cast(DDLJobStore, jobs),
            _record(JobStatus.WAITING_INPUT),
            "1-0",
        )
    )
    check_equal(
        "等待后事件序列",
        [
            frame.splitlines()[1]
            for frame in frames
        ],
        ["event: snapshot", "event: progress", "event: succeeded"],
    )
    check_equal("续接游标", jobs.cursors, ["1-0"])


async def test_missing_event_is_repaired_by_authoritative_snapshot() -> None:
    """阻塞超时后用权威终态快照修复状态与事件两步写入窗口。"""
    jobs = _EventJobs([[]], [_record(JobStatus.FAILED)])
    frames = await _collect(
        stream_job_events(
            cast(Request, _ConnectedRequest()),
            cast(DDLJobStore, jobs),
            _record(JobStatus.RUNNING),
            "4-0",
        )
    )
    check_equal(
        "修复快照序列",
        [frame.splitlines()[1] for frame in frames],
        ["event: snapshot", "event: snapshot"],
    )
    check_condition(
        "修复快照包含安全失败",
        '"code":"safe_failure"' in frames[-1],
        actual=frames[-1],
        expected="包含 JobRecord 公开错误",
    )


async def test_stream_failure_emits_fixed_safe_error_and_closes() -> None:
    """响应开始后的 Redis 故障只发送固定安全错误。"""
    jobs = _EventJobs(
        [RedisConnectionError("secret redis endpoint and password")],
    )
    frames = await _collect(
        stream_job_events(
            cast(Request, _ConnectedRequest()),
            cast(DDLJobStore, jobs),
            _record(JobStatus.RUNNING),
            "5-0",
        )
    )
    check_equal(
        "流故障事件序列",
        [frame.splitlines()[1] for frame in frames],
        ["event: snapshot", "event: stream_error"],
    )
    check_condition(
        "流故障不泄漏异常文本",
        "secret redis endpoint" not in frames[-1],
        actual=frames[-1],
        expected="只包含固定 stream_unavailable 错误",
    )


async def test_disconnected_client_stops_before_blocking_redis_read() -> None:
    """客户端断开后生成器不再持有阻塞 Redis 读取。"""
    jobs = _EventJobs([])
    frames = await _collect(
        stream_job_events(
            cast(Request, _DisconnectedRequest()),
            cast(DDLJobStore, jobs),
            _record(JobStatus.RUNNING),
            "6-0",
        )
    )
    check_equal("断开后仅保留已开始的快照", len(frames), 1)
    check_equal("断开后未启动 XREAD", jobs.cursors, [])


async def test_idle_stream_sends_heartbeat_and_can_be_closed() -> None:
    """空闲超时发送注释心跳，异步生成器可由响应层关闭。"""
    running = _record(JobStatus.RUNNING)
    jobs = _EventJobs([[]], [running])
    stream = stream_job_events(
        cast(Request, _ConnectedRequest()),
        cast(DDLJobStore, jobs),
        running,
        "7-0",
    )
    first = await anext(stream)
    heartbeat = await anext(stream)
    await stream.aclose()
    check_condition(
        "空闲流先发快照",
        "event: snapshot" in first,
        actual=first,
        expected="snapshot 帧",
    )
    check_equal("标准 SSE 注释心跳", heartbeat, ": heartbeat\n\n")
