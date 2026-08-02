"""DDL 任务 SSE 帧编码与可重连事件生成。"""

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

from fastapi import Request
from loguru import logger
from redis.exceptions import RedisError

from ddl_metadata.jobs.store import DDLJobStore
from errors import DataAgentError
from models.jobs import (
    JobError,
    JobEvent,
    JobEventData,
    JobEventStage,
    JobEventType,
    JobRecord,
    JobStatus,
)
from settings import app_config

_TERMINAL_EVENT_TYPES = {
    JobEventType.SUCCEEDED,
    JobEventType.REJECTED,
    JobEventType.FAILED,
}


def encode_sse(event: JobEvent) -> str:
    """把公开事件编码为单行 JSON 的标准 SSE 帧。"""
    return (
        f"id: {event.event_id}\n"
        f"event: {event.event_type.value}\n"
        f"data: {event.data.model_dump_json()}\n\n"
    )


async def stream_job_events(
    request: Request,
    jobs: DDLJobStore,
    record: JobRecord,
    cursor: str,
) -> AsyncGenerator[str, None]:
    """先发权威快照，再阻塞续接 Redis Stream 并修复缺失通知。"""
    # 步骤一：每次连接先以 Job Hash 的公开投影建立基线；Stream 只是通知日志，
    # 因此不能用客户端游标后的第一条通知替代当前权威状态。
    snapshot = jobs.snapshot_event(record, cursor)
    yield encode_sse(snapshot)
    if record.status in _TERMINAL_STATUSES:
        return

    last_data = snapshot.data
    last_projection = _data_projection(snapshot.data)
    block_milliseconds = max(
        1,
        round(app_config.api.sse_heartbeat_seconds * 1000),
    )
    try:
        while not await request.is_disconnected():
            # 步骤二：游标只随已发送事件前进，使断线重连可以继续读取未消费的通知。
            events = await jobs.read_events(
                record.job_id,
                cursor,
                block_milliseconds=block_milliseconds,
            )
            if events:
                for event in events:
                    cursor = event.event_id
                    last_data = event.data
                    last_projection = _data_projection(event.data)
                    yield encode_sse(event)
                    if event.event_type in _TERMINAL_EVENT_TYPES:
                        return
                continue

            if await request.is_disconnected():
                return
            # 步骤三：阻塞读取超时后重读权威 Hash；若状态转换后的通知发布失败，
            # 投影差异会补发 snapshot；无差异才发送不改变业务游标的心跳。
            latest = await jobs.get(record.job_id)
            repaired = jobs.snapshot_event(latest, cursor)
            projection = _data_projection(repaired.data)
            if projection != last_projection:
                last_data = repaired.data
                last_projection = projection
                yield encode_sse(repaired)
                if latest.status in _TERMINAL_STATUSES:
                    return
            else:
                yield ": heartbeat\n\n"
    except (RedisError, DataAgentError, ValueError):
        # 步骤四：响应启动后已无法改写 HTTP 状态，只发送基于最后公开坐标的固定错误，
        # 不泄露 Redis 异常或内部载荷，随后结束连接让客户端安全重连。
        logger.warning("DDL 任务事件流读取失败，客户端可使用最后公开游标安全重连")
        yield encode_sse(_stream_error(last_data, cursor))


def _stream_error(latest: JobEventData, cursor: str) -> JobEvent:
    """构造不包含内部异常文本的固定流错误事件。"""
    return JobEvent(
        event_id=cursor,
        event_type=JobEventType.STREAM_ERROR,
        data=JobEventData(
            job_id=latest.job_id,
            revision=latest.revision,
            attempt=latest.attempt,
            status=latest.status,
            stage=JobEventStage.STREAM_ERROR,
            emitted_at=datetime.now(UTC),
            error=JobError(
                code="stream_unavailable",
                stage="redis",
                retryable=True,
                attempt=latest.attempt,
            ),
        ),
    )


def _data_projection(data: JobEventData) -> str:
    """生成忽略发送时间和瞬时阶段的公开状态比较值。"""
    return data.model_dump_json(
        exclude={"emitted_at", "stage"},
    )


_TERMINAL_STATUSES = {
    JobStatus.SUCCEEDED,
    JobStatus.REJECTED,
    JobStatus.FAILED,
}
