"""FastAPI 应用组合与共享资源生命周期。"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from redis.exceptions import RedisError

from data_agent.answer_readiness.classifier import AnswerReadinessClassifier
from data_agent.answer_readiness.service import AnswerReadinessService
from data_agent.chat.api import router as chat_router
from data_agent.chat.service import ChatService
from data_agent.conversation.api import router as conversation_router
from data_agent.conversation.service import ConversationService
from data_agent.ddl_metadata.api.router import router as ddl_metadata_router
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.ddl_metadata.persistence.memory_references import (
    MetadataMemoryReferenceValidator,
)
from data_agent.errors import DataAgentError
from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.job_queue import build_queue_pool
from data_agent.infrastructure.llm_client import LLMClient
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

_LEGACY_FRONTEND_ENV = "ENABLE_LEGACY_FRONTEND"


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
    model = LLMClient.initialize()
    # 步骤三：构造 arq 队列客户端，使受理路径能立即调度激活而不必等待 worker 的
    # dispatch 周期；dispatch outbox 仍是崩溃兜底，入队失败时自动退回周期调度。
    # 这里走共享构造器而不用 arq.create_pool：后者在启动时就发起连接并带重试退避，
    # 会把"Redis 暂时不可达"从按请求失败升级为 API 无法启动，也让生命周期无法在
    # 没有 Redis 的环境下测试；而它同时漏掉读取超时。这是本进程族的第四个 Redis
    # 客户端，缺少读取超时时半开连接会让 submit 停在 queue.enqueue_job() 上永不
    # 返回，异常处理器也无法兜住"永不返回"的调用、退回 cron 调度。arq 需要字节
    # 响应，因此不能复用应用的解码客户端。
    # 连接重试预算取 0：立即调度失败由 _activate_now_safely() 退回 dispatch cron，
    # 在请求路径上耗尽整个连接预算只会把 HTTP 请求先挂死一遍，严格更差。
    queue = build_queue_pool(connect_retries=0)
    # 步骤四：把已就绪资源依赖的业务服务集中挂载到应用状态，供路由复用。
    jobs = DDLJobStore(redis, queue)
    app.state.jobs = jobs
    app.state.memories = MemoryService(jobs, MetadataMemoryReferenceValidator())
    conversations = ConversationService()
    app.state.conversations = conversations
    app.state.chat = ChatService(
        conversations,
        AnswerReadinessService(AnswerReadinessClassifier(model)),
        model,
    )
    # 步骤五：记录启动完成后把控制权交给 FastAPI，直至服务退出或运行异常。
    logger.info("API 服务已启动，数据库、缓存与派生检索资源均已就绪")
    try:
        yield
    finally:
        # 步骤六：按初始化逆序关闭外部资源，避免先释放仍被下游客户端依赖的资源。
        try:
            await LLMClient.close()
            await TEIEmbeddingClient.close()
            await QdrantClient.close()
            await ElasticsearchClient.close()
            await MySQLDatabase.close()
            await queue.aclose()
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


def _legacy_frontend_enabled() -> bool:
    """读取显式旧前端兼容开关，默认保持 API-only。"""
    # 步骤一：只接受清晰布尔值，避免拼写错误意外恢复已弃用的生产入口。
    raw_value = os.environ.get(_LEGACY_FRONTEND_ENV, "false").strip().casefold()
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(
        f"{_LEGACY_FRONTEND_ENV} 只能使用 true/false、1/0、yes/no 或 on/off"
    )


def _mount_legacy_frontend(app: FastAPI) -> None:
    """仅在迁移期开关启用时挂载已弃用的内嵌前端。"""
    # 步骤一：延迟导入静态服务依赖，保证 API-only 启动不读取前端源码目录。
    from pathlib import Path

    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles

    frontend_dir = Path(__file__).with_name("frontend")
    if not frontend_dir.is_dir():
        raise RuntimeError("旧前端目录不存在，无法启用 ENABLE_LEGACY_FRONTEND")
    # 步骤二：记录迁移提示并恢复旧路径，供独立前端切换窗口内紧急回滚。
    logger.warning(
        "旧内嵌前端兼容入口已启用；该入口将在独立前端迁移完成后删除，"
        "生产环境请保持 ENABLE_LEGACY_FRONTEND=false"
    )
    app.mount(
        "/assets",
        StaticFiles(directory=frontend_dir),
        name="legacy-frontend-assets",
    )

    @app.get("/", include_in_schema=False)
    @app.get("/workbench", include_in_schema=False)
    @app.get("/workbench/{job_id}", include_in_schema=False)
    @app.get("/knowledge", include_in_schema=False)
    async def legacy_frontend(job_id: str | None = None) -> FileResponse:
        """返回迁移期保留的旧 Schema Loom 单页入口。"""
        del job_id
        return FileResponse(frontend_dir / "index.html")


def create_app() -> FastAPI:
    """创建默认 API-only、仅面向本机浏览器的应用。"""
    # 步骤一：创建带生命周期的应用，并按已校验配置装配本机浏览器 CORS 边界。
    app = FastAPI(title="Data Agent DDL Metadata API", lifespan=_observed_lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            str(origin).rstrip("/") for origin in app_config.api.cors_origins
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "Idempotency-Key"],
    )
    # 步骤二：集中注册安全异常投影和业务路由，保持传输层入口只有一个组合根。
    app.add_middleware(RequestLoggingContextMiddleware)
    app.add_exception_handler(DataAgentError, _handle_business_error)
    app.add_exception_handler(RedisError, _handle_redis_error)
    app.include_router(ddl_metadata_router)
    app.include_router(conversation_router)
    app.include_router(chat_router)

    @app.get("/api/v1/health", tags=["health"])
    async def health() -> dict[str, object]:
        """返回不触发外部依赖访问的 API 进程存活状态。"""
        return {
            "status": "ok",
            "capabilities": {"ddl_submission_idempotency": True},
        }

    # 步骤三：兼容开关默认关闭；启用时才读取并挂载旧 Python 包内前端目录。
    if _legacy_frontend_enabled():
        _mount_legacy_frontend(app)
    # 步骤四：返回完成 API 装配的应用；外部资源仍由启动阶段的 lifespan 初始化。
    return app
