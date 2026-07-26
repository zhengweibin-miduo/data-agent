"""FastAPI 应用组合与共享资源生命周期。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from redis.exceptions import RedisError

from data_agent.conversation.api import router as conversation_router
from data_agent.conversation.service import ConversationService
from data_agent.ddl_metadata.api.router import router as ddl_metadata_router
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.ddl_metadata.persistence.memory_references import (
    MetadataMemoryReferenceValidator,
)
from data_agent.errors import DataAgentError
from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.infrastructure.redis import RedisClient
from data_agent.infrastructure.tei_embeddings import TEIEmbeddingClient
from data_agent.logging import (
    RequestLoggingContextMiddleware,
    logging_boundary,
    setup_logging,
)
from data_agent.memory.application.service import MemoryService
from data_agent.settings import app_config


async def _lifespan_resources(app: FastAPI) -> AsyncIterator[None]:
    """管理 API 资源，并在资源就绪后装配业务服务。

    外部客户端初始化完成后，才将依赖它们的业务服务装配到 ``app.state``；
    退出时按初始化的逆序调用各资源管理器的关闭入口。
    """
    # 步骤一：先重建日志 sinks，使后续资源初始化与关闭过程使用统一日志上下文。
    setup_logging()
    # 步骤二：按依赖顺序初始化共享外部资源，全部就绪后才允许装配业务服务。
    redis = RedisClient.initialize()
    MySQLDatabase.initialize()
    ElasticsearchClient.initialize()
    QdrantClient.initialize()
    TEIEmbeddingClient.initialize()
    # 步骤三：把已就绪资源依赖的业务服务集中挂载到应用状态，供路由复用。
    jobs = DDLJobStore(redis)
    app.state.jobs = jobs
    app.state.memories = MemoryService(jobs, MetadataMemoryReferenceValidator())
    app.state.conversations = ConversationService()
    # 步骤四：记录启动完成后把控制权交给 FastAPI，直至服务退出或运行异常。
    logger.info("API 服务已启动，数据库、缓存与派生检索资源均已就绪")
    try:
        yield
    finally:
        # 步骤五：按初始化逆序关闭外部资源，避免先释放仍被下游客户端依赖的资源。
        try:
            await TEIEmbeddingClient.close()
            await QdrantClient.close()
            await ElasticsearchClient.close()
            await MySQLDatabase.close()
            await RedisClient.close()
            logger.info("API 服务已停止，进程内共享资源已经关闭")
        finally:
            # 步骤六：无论资源关闭是否异常，都等待异步日志队列完成已接收记录的写出。
            await logger.complete()


_lifespan = asynccontextmanager(_lifespan_resources)
_observed_lifespan = asynccontextmanager(logging_boundary()(_lifespan_resources))


async def _handle_business_error(
    request: Request,
    error: Exception,
) -> JSONResponse:
    """把稳定业务错误映射为安全 HTTP 响应。"""
    del request
    if not isinstance(error, DataAgentError):
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
    # 步骤一：创建带生命周期的应用，并按已校验配置装配本机浏览器 CORS 边界。
    app = FastAPI(title="Data Agent DDL Metadata API", lifespan=_observed_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            str(origin).rstrip("/") for origin in app_config.api.cors_origins
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type"],
    )
    # 步骤二：集中注册安全异常投影和业务路由，保持传输层入口只有一个组合根。
    app.add_middleware(RequestLoggingContextMiddleware)
    app.add_exception_handler(DataAgentError, _handle_business_error)
    app.add_exception_handler(RedisError, _handle_redis_error)
    app.include_router(ddl_metadata_router)
    app.include_router(conversation_router)
    # 步骤三：返回完成静态装配的应用；外部资源仍由启动阶段的 lifespan 初始化。
    return app
