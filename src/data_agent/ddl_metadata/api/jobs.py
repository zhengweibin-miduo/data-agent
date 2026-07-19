"""DDL 元数据任务 HTTP 路由。"""

from fastapi import APIRouter, Request, status
from loguru import logger

from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.ddl_metadata.models.jobs import (
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
    """持久受理 DDL 元数据任务。"""
    record = await _jobs(request).submit(body)
    logger.bind(
        trace_id=record.job_id,
        component="ddl_metadata.api",
        event_name="ddl_metadata.job.accepted",
        operation="submit_job",
        outcome="accepted",
        job_status=record.status.value,
        revision=record.revision,
    ).info("DDL 元数据任务已受理")
    return DDLJobAccepted(
        job_id=record.job_id,
        status_url=f"/api/v1/metadata/ddl-jobs/{record.job_id}",
    )


@router.get(
    "/api/v1/metadata/ddl-jobs/{job_id}",
    response_model=JobRecord,
)
async def get_job(job_id: str, request: Request) -> JobRecord:
    """读取安全的公开任务投影。"""
    return await _jobs(request).get(job_id)


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
    record, accepted = await _jobs(request).submit_answers(job_id, body)
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
    return record
