"""FastAPI 应用工厂与本地元数据路由。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

from app.client.mysql_client_manager import MysqlClientManager
from app.client.redis_client_manager import RedisClientManager
from app.conf.app_config import app_config
from app.core.logging import setup_logging
from app.model.ddl_metadata import (
    AnswerRequest,
    DdlJobAccepted,
    DdlJobRequest,
    JobRecord,
    MemoryCorrectionRequest,
    MemoryCorrectionResponse,
    MemoryDetail,
    MemoryKind,
    MemoryPage,
    MemoryPatchRequest,
    MemoryRowStatus,
)
from app.service.ddl_metadata.errors import DdlMetadataError
from app.service.ddl_metadata.job_store import JobStore
from app.service.ddl_metadata.memory_management import MemoryManagementService


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """显式管理 API 进程内的 Redis/MySQL 生命周期。"""
    setup_logging()
    redis = RedisClientManager.initialize()
    MysqlClientManager.initialize()
    jobs = JobStore(redis)
    app.state.jobs = jobs
    app.state.memories = MemoryManagementService(jobs)
    try:
        yield
    finally:
        await MysqlClientManager.close()
        await RedisClientManager.close()


def create_app() -> FastAPI:
    """创建仅面向本机浏览器的应用。"""
    app = FastAPI(
        title="Data Agent DDL Metadata API",
        lifespan=_lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[str(origin).rstrip("/") for origin in app_config.api.cors_origins],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Content-Type"],
    )

    @app.exception_handler(DdlMetadataError)
    async def handle_business_error(
        request: Request,
        error: DdlMetadataError,
    ) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=error.http_status,
            content={
                "error": {
                    "code": error.code,
                    "stage": error.stage,
                    "retryable": error.retryable,
                    "details": error.details,
                }
            },
        )

    @app.exception_handler(RedisError)
    async def handle_redis_error(
        request: Request,
        error: RedisError,
    ) -> JSONResponse:
        del request, error
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "redis_unavailable",
                    "stage": "redis",
                    "retryable": True,
                    "details": {},
                }
            },
        )

    def jobs(request: Request) -> JobStore:
        return request.app.state.jobs

    def memories(request: Request) -> MemoryManagementService:
        return request.app.state.memories

    @app.post(
        "/api/v1/metadata/ddl-jobs",
        response_model=DdlJobAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit_job(
        body: DdlJobRequest,
        request: Request,
    ) -> DdlJobAccepted:
        record = await jobs(request).submit(body)
        return DdlJobAccepted(
            job_id=record.job_id,
            status_url=f"/api/v1/metadata/ddl-jobs/{record.job_id}",
        )

    @app.get(
        "/api/v1/metadata/ddl-jobs/{job_id}",
        response_model=JobRecord,
    )
    async def get_job(job_id: str, request: Request) -> JobRecord:
        return await jobs(request).get(job_id)

    @app.post(
        "/api/v1/metadata/ddl-jobs/{job_id}/answers",
        response_model=JobRecord,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def answer_job(
        job_id: str,
        body: AnswerRequest,
        request: Request,
    ) -> JobRecord:
        record, _accepted = await jobs(request).submit_answers(job_id, body)
        return record

    @app.get(
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
        return await memories(request).list_page(
            source,
            kind=kind,
            row_status=row_status,
            pinned=pinned,
            limit=limit,
            cursor=cursor,
        )

    @app.get(
        "/api/v1/metadata/memories/{memory_uid}",
        response_model=MemoryDetail,
    )
    async def get_memory(
        memory_uid: str,
        request: Request,
    ) -> MemoryDetail:
        return await memories(request).get_detail(memory_uid)

    @app.patch(
        "/api/v1/metadata/memories/{memory_uid}",
        response_model=MemoryDetail,
    )
    async def patch_memory(
        memory_uid: str,
        body: MemoryPatchRequest,
        request: Request,
    ) -> MemoryDetail:
        return await memories(request).patch(memory_uid, body)

    @app.post(
        "/api/v1/metadata/memories/{memory_uid}/corrections",
        response_model=MemoryCorrectionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def correct_memory(
        memory_uid: str,
        body: MemoryCorrectionRequest,
        request: Request,
    ) -> MemoryCorrectionResponse:
        return await memories(request).correct(memory_uid, body.content)

    return app
