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
    MemoryDeleteResponse,
    MemoryDetail,
    MemoryHistoryPage,
    MemoryKind,
    MemorySearchResponse,
    MemoryUpdateRequest,
    MemoryUpdateResponse,
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
    "/api/v1/metadata/memories/search",
    response_model=MemorySearchResponse,
)
async def search_memories(
    request: Request,
    query: str = Query(min_length=1, max_length=2000),
    source: str = Query(min_length=1, max_length=128),
    kind: list[MemoryKind] | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> MemorySearchResponse:
    """按来源、查询和可选类型执行混合检索。"""
    return await _memories(request).search(
        query,
        source,
        kinds=set(kind) if kind else None,
        limit=limit,
    )


@router.get(
    "/api/v1/metadata/memories/{memory_uid}",
    response_model=MemoryDetail,
)
async def get_memory(memory_uid: str, request: Request) -> MemoryDetail:
    """从 MySQL 读取一条权威记忆详情。"""
    return await _memories(request).get(memory_uid)


@router.get(
    "/api/v1/metadata/memories/{memory_uid}/history",
    response_model=MemoryHistoryPage,
)
async def get_memory_history(
    memory_uid: str,
    request: Request,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
) -> MemoryHistoryPage:
    """读取记忆的有界只追加历史。"""
    return await _memories(request).history(
        memory_uid,
        offset=offset,
        limit=limit,
    )


@router.patch(
    "/api/v1/metadata/memories/{memory_uid}",
    response_model=MemoryUpdateResponse,
)
async def update_memory(
    memory_uid: str,
    body: MemoryUpdateRequest,
    request: Request,
) -> MemoryUpdateResponse:
    """记录结构化用户修正并要求重新处理 DDL。"""
    return await _memories(request).update(memory_uid, body.content)


@router.delete(
    "/api/v1/metadata/memories/{memory_uid}",
    response_model=MemoryDeleteResponse,
)
async def delete_memory(
    memory_uid: str,
    request: Request,
) -> MemoryDeleteResponse:
    """执行可审计软删除并排除未来召回。"""
    return await _memories(request).delete(memory_uid)
