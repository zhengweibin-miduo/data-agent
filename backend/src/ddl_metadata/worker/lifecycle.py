"""DDL 元数据 worker 资源生命周期与图装配。"""

import asyncio
from typing import Any, cast

from arq.connections import ArqRedis, RedisSettings
from loguru import logger
from redis.exceptions import RedisError

from app_logging import setup_logging
from conversation.adapters.extraction_model import StructuredExtractionModel
from conversation.adapters.mysql.extraction import (
    MySQLExtractionClaimStore,
    MySQLExtractionCommitter,
)
from conversation.application.extraction import ConversationMemoryExtractor
from ddl_metadata.adapters.mysql.accepted_snapshot import (
    MySQLAcceptedSnapshotPublisher,
)
from ddl_metadata.jobs.store import DDLJobStore
from ddl_metadata.meta_projection.adapters.composition import (
    compose_meta_projection_runtime,
)
from ddl_metadata.meta_projection.elasticsearch import (
    MetadataValueElasticsearchIndex,
)
from ddl_metadata.meta_projection.qdrant import MetadataQdrantIndex
from ddl_metadata.worker.maintenance import (
    cleanup_checkpoints,
    dispatch_pending,
)
from ddl_metadata.workflow.contracts import DDLGraphDependencies
from ddl_metadata.workflow.graph import build_ddl_metadata_graph
from ddl_metadata.workflow.llm_metadata_generator import (
    LLMMetadataGenerator,
)
from ddl_metadata.workflow.memory_context import MemoryContextLoader
from errors import DataAgentError
from infrastructure.checkpoint_store import CheckpointStore
from infrastructure.elasticsearch import ElasticsearchClient
from infrastructure.generation_locks import GenerationLockManager
from infrastructure.llm_client import LLMClient
from infrastructure.mysql import MySQLDatabase
from infrastructure.qdrant import QdrantClient
from infrastructure.redis import RedisClient
from infrastructure.tei_embeddings import TEIEmbeddingClient
from memory.adapters.composition import build_memory_worker_runtime
from memory.indexing.elasticsearch import (
    MemoryElasticsearchIndex,
)
from memory.indexing.qdrant import MemoryQdrantIndex
from settings import app_config


async def _wait_for_queue(queue: ArqRedis) -> None:
    """按有界重试等待 arq 队列连接可用。

    与 `arq.create_pool` 的启动语义对齐：先 ping，失败则按固定间隔重试有限次，
    仍不可用才让异常传播。Redis 短暂未就绪时 worker 因此不会直接退出。

    Args:
        queue: worker 使用的 arq 队列客户端。

    Raises:
        Exception: 重试耗尽后仍无法连接时传播最后一次异常。
    """
    settings = RedisSettings.from_dsn(app_config.redis.url)
    attempts = max(settings.conn_retries, 1)
    # 步骤一：逐次探测连通性，成功即返回。
    for attempt in range(1, attempts + 1):
        try:
            await queue.ping()
            return
        except (RedisError, OSError):
            # 步骤二：最后一次仍失败则让异常传播，由部署方决定重启策略。
            if attempt == attempts:
                raise
            logger.warning("arq 队列连接暂不可用，按启动重试策略等待后重试")
            await asyncio.sleep(settings.conn_retry_delay)


def is_fatal_index_error(error: BaseException) -> bool:
    """判定索引初始化异常是否必须阻断 worker 启动。

    结构不兼容不会自行痊愈，必须由运维显式重建索引。长期记忆索引的临时连接失败
    仍沿用既有降级契约；Meta 索引 setup 则在调用本函数前已按更严格契约直接失败。

    Args:
        error: 索引初始化抛出的异常。

    Returns:
        是否属于必须阻断启动的结构性故障。
    """
    return isinstance(error, DataAgentError) and error.code in {
        "memory_index_mapping_invalid",
        "metadata_semantic_mapping_invalid",
        "metadata_value_mapping_invalid",
    }


async def startup(ctx: dict[Any, Any]) -> None:
    """显式初始化 worker 的全部长生命周期依赖。"""
    # 步骤一：先配置日志并初始化任务、数据库及派生索引所需的外部客户端。
    setup_logging()
    # 预置 redis_pool 后 arq 不再走 create_pool，也就失去了它启动时的连通性探测与
    # 有界重试；Redis 尚未就绪时进程会在首次队列操作直接退出。这里补回等价语义。
    await _wait_for_queue(cast(ArqRedis, ctx["redis"]))
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
    ctx["generation_locks"] = generation_locks
    elasticsearch = ElasticsearchClient.initialize()
    qdrant = QdrantClient.initialize()
    embeddings = TEIEmbeddingClient.initialize()
    # 步骤二：Meta 索引结构必须先成功创建或严格校验，才允许处理 Meta/DW 业务数据。
    for setup in (
        MetadataValueElasticsearchIndex(elasticsearch).setup,
        MetadataQdrantIndex(qdrant).setup,
    ):
        await setup()
    # 步骤三：长期记忆索引沿用既有降级契约；结构不兼容仍阻断启动。
    for setup in (
        MemoryElasticsearchIndex(elasticsearch).setup,
        MemoryQdrantIndex(qdrant).setup,
    ):
        try:
            await setup()
        except Exception as error:
            if is_fatal_index_error(error):
                raise
            logger.warning("记忆索引初始化失败，本次启动继续运行并将在后续启动时重试")
    # 步骤四：索引客户端就绪后一次构造长生命周期 Memory worker 用例并注入 ctx。
    memory_runtime = build_memory_worker_runtime(
        elasticsearch_client=elasticsearch,
        qdrant_client=qdrant,
        embeddings=embeddings,
    )
    ctx["memory_dispatcher"] = memory_runtime.dispatcher
    ctx["memory_maintenance"] = memory_runtime.maintenance
    # 步骤五：模型能力探测与 checkpoint 初始化成功后，才装配任务门面和工作流。
    model = LLMClient.initialize()
    await LLMClient.check_structured_output_capability()
    checkpointer = await CheckpointStore.initialize()
    jobs = DDLJobStore(redis)
    # 活动索引只由本版本的原子脚本维护，升级前停在 pending/running 的任务不在其中，
    # 而非终态 Hash 不设 TTL 不会自行消失；启动补录一次使停滞巡检能覆盖它们。
    backfilled = await jobs.backfill_active_index()
    if backfilled:
        logger.info("已把遗留非终态任务补录进活动索引，停滞巡检将覆盖它们")
    ctx["jobs"] = jobs
    ctx["conversation_extractor"] = ConversationMemoryExtractor(
        StructuredExtractionModel(
            model,
            method=app_config.llm.structured_output_method,
        ),
        MySQLExtractionClaimStore(
            max_backoff_seconds=app_config.memory.outbox_max_backoff_seconds
        ),
        MySQLExtractionCommitter(),
        batch_size=app_config.conversation.extraction_batch_size,
        max_concurrency=app_config.llm.max_concurrency,
        lease_seconds=app_config.conversation.extraction_lease_seconds,
        message_limit=app_config.conversation.context_message_limit,
        summary_max_chars=app_config.conversation.summary_max_chars,
        content_version=app_config.memory.content_version,
        projection_version=app_config.memory.projection_version,
    )
    metadata_projection = compose_meta_projection_runtime(
        elasticsearch=elasticsearch,
        qdrant=qdrant,
        embeddings=embeddings,
    )
    ctx["metadata_projection"] = metadata_projection
    ctx["metadata_index_dispatcher"] = metadata_projection.dispatcher
    ctx["graph"] = build_ddl_metadata_graph(
        DDLGraphDependencies(
            model=LLMMetadataGenerator(),
            memory_context=MemoryContextLoader(),
            snapshot_publisher=MySQLAcceptedSnapshotPublisher(generation_locks),
        ),
        checkpointer,
    )
    # 步骤六：服务就绪前先重放 dispatch 与 checkpoint 清理 outbox，再报告启动完成。
    await dispatch_pending(ctx)
    await cleanup_checkpoints(ctx)
    logger.info("DDL 元数据 worker 已启动，任务执行与周期维护资源均已就绪")


async def shutdown(ctx: dict[Any, Any]) -> None:
    """按依赖逆序关闭 worker 资源。"""
    # 步骤一：丢弃进程上下文引用，并按依赖逆序释放所有长生命周期资源。
    generation_locks = cast(
        GenerationLockManager | None, ctx.pop("generation_locks", None)
    )
    try:
        await CheckpointStore.close()
        await LLMClient.close()
        await TEIEmbeddingClient.close()
        await QdrantClient.close()
        await ElasticsearchClient.close()
        if generation_locks is not None:
            await generation_locks.close()
        await MySQLDatabase.close()
        await RedisClient.close()
        logger.info("DDL 元数据 worker 已停止，进程内共享资源已经关闭")
    # 步骤二：无论资源关闭是否抛错，最后都刷新日志队列。
    finally:
        await logger.complete()
