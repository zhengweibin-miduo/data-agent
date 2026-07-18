"""FastAPI 应用组合与共享资源生命周期。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from redis.exceptions import RedisError

from data_agent.ddl_metadata.api import router as ddl_metadata_router
from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.ddl_metadata.memory.service import MemoryService
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.redis import RedisClient
from data_agent.logging import setup_logging
from data_agent.settings import app_config


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """显式管理 API 进程内的 Redis/MySQL 生命周期。"""
    setup_logging()
    redis = RedisClient.initialize()
    MySQLDatabase.initialize()
    jobs = DDLJobStore(redis)
    app.state.jobs = jobs
    app.state.memories = MemoryService(jobs)
    logger.bind(
        component="application.api",
        event_name="application.lifecycle.started",
        operation="serve_api",
        outcome="started",
        worker_role="api",
    ).info("API 服务已启动")
    try:
        yield
    finally:
        await MySQLDatabase.close()
        await RedisClient.close()
        logger.bind(
            component="application.api",
            event_name="application.lifecycle.stopped",
            operation="serve_api",
            outcome="stopped",
            worker_role="api",
        ).info("API 服务已停止")


async def _handle_business_error(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """把稳定业务错误映射为安全 HTTP 响应。"""
    del request
    if not isinstance(error, DDLMetadataError):
        raise error
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


async def _handle_redis_error(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """把 Redis 边界故障映射为 503。"""
    del request
    if not isinstance(error, RedisError):
        raise error
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


def create_app() -> FastAPI:
    """创建仅面向本机浏览器的应用。"""
    app = FastAPI(title="Data Agent DDL Metadata API", lifespan=_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            str(origin).rstrip("/") for origin in app_config.api.cors_origins
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Content-Type"],
    )
    app.add_exception_handler(DDLMetadataError, _handle_business_error)
    app.add_exception_handler(RedisError, _handle_redis_error)
    app.include_router(ddl_metadata_router)
    return app
