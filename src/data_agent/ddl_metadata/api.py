"""DDL 元数据功能的本地 HTTP 路由。"""

from fastapi import APIRouter, Query, Request, status
from loguru import logger

from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.ddl_metadata.memory.service import MemoryService
from data_agent.ddl_metadata.models import (
    AnswerRequest,
    DDLJobAccepted,
    DDLJobRequest,
    JobRecord,
    MemoryCorrectionRequest,
    MemoryCorrectionResponse,
    MemoryDetail,
    MemoryKind,
    MemoryPage,
    MemoryPatchRequest,
    MemoryRowStatus,
)

router = APIRouter()


def _jobs(request: Request) -> DDLJobStore:
    """读取应用生命周期创建的任务存储。"""
    return request.app.state.jobs


def _memories(request: Request) -> MemoryService:
    """读取应用生命周期创建的记忆服务。"""
    return request.app.state.memories


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


@router.get(
    "/api/v1/metadata/memories",
    response_model=MemoryPage,
)
async def list_memories(
    request: Request,
    source: str = Query(min_length=1, max_length=128),
    kind: MemoryKind | None = None,
    row_status: MemoryRowStatus = MemoryRowStatus.NORMAL,
    pinned: bool | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
) -> MemoryPage:
    """按来源和有界过滤条件分页列出记忆。"""
    return await _memories(request).list_page(
        source,
        kind=kind,
        row_status=row_status,
        pinned=pinned,
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/api/v1/metadata/memories/{memory_uid}",
    response_model=MemoryDetail,
)
async def get_memory(memory_uid: str, request: Request) -> MemoryDetail:
    """读取一条有界记忆详情。"""
    return await _memories(request).get_detail(memory_uid)


@router.patch(
    "/api/v1/metadata/memories/{memory_uid}",
    response_model=MemoryDetail,
)
async def patch_memory(
    memory_uid: str,
    body: MemoryPatchRequest,
    request: Request,
) -> MemoryDetail:
    """幂等修改记忆的 pin 或 archive 状态。"""
    return await _memories(request).patch(memory_uid, body)


@router.post(
    "/api/v1/metadata/memories/{memory_uid}/corrections",
    response_model=MemoryCorrectionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def correct_memory(
    memory_uid: str,
    body: MemoryCorrectionRequest,
    request: Request,
) -> MemoryCorrectionResponse:
    """追加用户确认的记忆修正。"""
    return await _memories(request).correct(memory_uid, body.content)
