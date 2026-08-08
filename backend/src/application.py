"""FastAPI 应用组合与共享资源生命周期。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from redis.exceptions import RedisError

from answer_readiness.classifier import AnswerReadinessClassifier
from answer_readiness.service import AnswerReadinessService
from answer_readiness.tool import create_data_readiness_tool
from app_logging import (
    RequestLoggingContextMiddleware,
    logging_boundary,
    setup_logging,
)
from chat.api import router as chat_router
from chat.service import ChatService
from conversation.adapters.long_term_memory import (
    MemorySearchLongTermMemoryReader,
)
from conversation.adapters.mysql.store import MySQLConversationStore
from conversation.adapters.mysql.user_data import MySQLUserDataEraser
from conversation.api import router as conversation_router
from conversation.application.service import ConversationService
from ddl_metadata.api.router import router as ddl_metadata_router
from ddl_metadata.jobs.store import DDLJobStore
from ddl_metadata.meta_projection.adapters.composition import (
    compose_meta_projection_runtime,
)
from ddl_metadata.persistence.memory_references import (
    MetadataMemoryReferenceValidator,
)
from errors import DataAgentError
from infrastructure.elasticsearch import ElasticsearchClient
from infrastructure.generation_locks import GenerationLockManager
from infrastructure.job_queue import build_queue_pool
from infrastructure.llm_client import LLMClient
from infrastructure.mysql import MySQLDatabase
from infrastructure.qdrant import QdrantClient
from infrastructure.redis import RedisClient
from infrastructure.tei_embeddings import TEIEmbeddingClient
from memory.adapters.composition import build_memory_runtime
from query.adapters.http import router as query_router
from query.adapters.llm import QueryLLMAdapter
from query.adapters.metadata import QueryMetadataAdapter
from query.adapters.mysql import MySQLQueryExecutor
from query.adapters.readiness import QueryReadinessAdapter
from query.application.service import QueryApplication
from settings import app_config


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
    generation_locks = GenerationLockManager(
        app_config.mysql.url,
        pool_size=app_config.mysql.generation_lock_pool_size,
        pool_timeout_seconds=app_config.mysql.generation_lock_pool_timeout_seconds,
        io_timeout_seconds=app_config.mysql.generation_lock_io_timeout_seconds,
    )
    await generation_locks.initialize()
    try:
        await generation_locks.check_capability()
    except BaseException:
        await generation_locks.close()
        raise
    elasticsearch = ElasticsearchClient.initialize()
    qdrant = QdrantClient.initialize()
    embeddings = TEIEmbeddingClient.initialize()
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
    memory_runtime = build_memory_runtime(
        jobs,
        MetadataMemoryReferenceValidator(),
        elasticsearch=elasticsearch,
        qdrant=qdrant,
        embeddings=embeddings,
    )
    app.state.memories = memory_runtime.service
    conversations = ConversationService(
        MySQLConversationStore(),
        MemorySearchLongTermMemoryReader(memory_runtime.search),
        MySQLUserDataEraser(),
        context_message_limit=app_config.conversation.context_message_limit,
        context_max_chars=app_config.conversation.context_max_chars,
        summary_max_chars=app_config.conversation.summary_max_chars,
        memory_search_limit=app_config.memory.search_limit,
    )
    app.state.conversations = conversations
    app.state.chat = ChatService(
        conversations,
        AnswerReadinessService(AnswerReadinessClassifier(model)),
        model,
        turn_lease_seconds=app_config.conversation.turn_lease_seconds,
    )
    meta_projection = compose_meta_projection_runtime(
        elasticsearch=elasticsearch,
        qdrant=qdrant,
        embeddings=embeddings,
    )
    query_model = QueryLLMAdapter(
        model,
        dw_database=app_config.data_sync.dw_database,
    )
    query_executor = MySQLQueryExecutor(
        app_config.query.read_url,
        timeout_seconds=app_config.query.timeout_seconds,
        fetch_batch_rows=app_config.query.fetch_batch_rows,
        max_batch_bytes=app_config.query.max_batch_bytes,
    )
    app.state.query = QueryApplication(
        conversations=conversations,
        intents=query_model,
        metadata=QueryMetadataAdapter(meta_projection.search),
        planner=query_model,
        readiness=QueryReadinessAdapter(
            create_data_readiness_tool(),
            generation_locks,
            dw_database=app_config.data_sync.dw_database,
            lock_timeout=app_config.data_sync.generation_lock_timeout_seconds,
        ),
        executor=query_executor,
        dw_database=app_config.data_sync.dw_database,
        control_io_timeout_seconds=app_config.query.timeout_seconds,
        turn_lease_seconds=app_config.conversation.turn_lease_seconds,
        clarification_chain_message_limit=(
            app_config.query.clarification_chain_message_limit
        ),
        clarification_chain_max_chars=app_config.query.clarification_chain_max_chars,
    )
    # 步骤五：记录启动完成后把控制权交给 FastAPI，直至服务退出或运行异常。
    logger.info("API 服务已启动，数据库、缓存与派生检索资源均已就绪")
    try:
        yield
    finally:
        # 步骤六：按初始化逆序关闭外部资源，避免先释放仍被下游客户端依赖的资源。
        try:
            await query_executor.close()
            await LLMClient.close()
            await TEIEmbeddingClient.close()
            await QdrantClient.close()
            await ElasticsearchClient.close()
            await generation_locks.close()
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
    app.include_router(query_router)

    @app.get("/api/v1/health", tags=["health"])
    async def health() -> dict[str, object]:
        """返回不触发外部依赖访问的 API 进程存活状态。"""
        return {
            "status": "ok",
            "capabilities": {"ddl_submission_idempotency": True},
        }

    # 步骤三：返回完成 API 装配的应用；外部资源仍由启动阶段的 lifespan 初始化。
    return app
