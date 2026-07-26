"""DDL 元数据任务 HTTP 路由。"""

from fastapi import APIRouter, Request, status
from fastapi.responses import StreamingResponse
from loguru import logger

from data_agent.ddl_metadata.api.job_events import stream_job_events
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.models.jobs import (
    AnswerRequest,
    DDLJobAccepted,
    DDLJobRequest,
    JobRecord,
)

router = APIRouter()


def _jobs(request: Request) -> DDLJobStore:
    """读取应用生命周期创建的任务存储。"""
    return request.app.state.jobs


@router.post(
    "/api/v1/metadata/ddl-jobs",
    response_model=DDLJobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_job(body: DDLJobRequest, request: Request) -> DDLJobAccepted:
    """持久受理任务并返回异步查询入口。

    ``202`` 表示任务状态、来源租约与 dispatch outbox 已原子写入 Redis，
    不表示 LangGraph 已开始或完成；实际执行由 worker 从 outbox 异步调度。
    """
    # 步骤一：先由任务门面原子建立权威状态、来源租约与 dispatch outbox，
    # 只有持久受理成功后才继续构造 HTTP 202 响应。
    record = await _jobs(request).submit(body)
    # 步骤二：记录不含原始 DDL 的安全受理坐标，供异步执行链路关联排查。
    logger.bind(
        trace_id=record.job_id,
        component="ddl_metadata.api",
        event_name="ddl_metadata.job.accepted",
        operation="submit_job",
        outcome="accepted",
        job_status=record.status.value,
        revision=record.revision,
    ).info("DDL 元数据任务已受理")
    # 步骤三：返回轮询与 SSE 地址，把后续执行和结果读取留给异步客户端。
    return DDLJobAccepted(
        job_id=record.job_id,
        status_url=f"/api/v1/metadata/ddl-jobs/{record.job_id}",
        events_url=f"/api/v1/metadata/ddl-jobs/{record.job_id}/events",
    )


@router.get(
    "/api/v1/metadata/ddl-jobs/{job_id}",
    response_model=JobRecord,
)
async def get_job(job_id: str, request: Request) -> JobRecord:
    """读取安全的公开任务投影。"""
    return await _jobs(request).get(job_id)


@router.get("/api/v1/metadata/ddl-jobs/{job_id}/events")
async def get_job_events(
    job_id: str,
    request: Request,
) -> StreamingResponse:
    """订阅只读、可重连的 DDL 任务公开事件流。

    首帧依据权威任务投影生成，后续 Stream 只承载公开通知；LangGraph 的
    节点输入、输出与 interrupt 载荷不会越过 SSE 边界。
    """
    jobs = _jobs(request)
    # 步骤一：在响应启动前校验任务存在，保留标准 404/503 HTTP 错误投影。
    await jobs.get(job_id)
    # 步骤二：以当前通知流尾部作为连接游标，避免把历史通知误当作新事件。
    cursor = await jobs.event_tail_id(job_id)
    # 步骤三：再次读取权威 Job Hash，确保首帧快照覆盖游标确定期间的状态变化。
    record = await jobs.get(job_id)
    # 步骤四：启动只输出公开投影的 SSE 生成器，并关闭代理缓冲以支持实时消费。
    return StreamingResponse(
        stream_job_events(request, jobs, record, cursor),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/api/v1/metadata/ddl-jobs/{job_id}/answers",
    response_model=JobRecord,
    status_code=status.HTTP_202_ACCEPTED,
)
async def answer_job(
    job_id: str,
    body: AnswerRequest,
    request: Request,
) -> JobRecord:
    """提交当前修订的问题回答。"""
    # 步骤一：由任务门面原子校验当前问题集、修订和截止时间，并安排下一修订。
    record, accepted = await _jobs(request).submit_answers(job_id, body)
    # 步骤二：仅对首次受理记录安全审计日志，幂等重放直接复用权威结果。
    if accepted:
        logger.bind(
            trace_id=job_id,
            component="ddl_metadata.api",
            event_name="ddl_metadata.job.answers_submitted",
            operation="submit_answers",
            outcome="accepted",
            job_status=record.status.value,
            revision=record.revision,
            question_round=record.question_round,
            question_count=len(body.answers),
        ).info("指标问题回答已受理")
    # 步骤三：返回脚本裁决后的最新公开投影，供客户端继续轮询或订阅事件。
    return record
