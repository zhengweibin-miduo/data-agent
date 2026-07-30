"""DDL 元数据 worker 资源生命周期与图装配。"""

import asyncio
from typing import Any, cast

from arq.connections import ArqRedis, RedisSettings
from loguru import logger
from redis.exceptions import RedisError

from data_agent.conversation.extraction import ConversationMemoryExtractor
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.ddl_metadata.persistence.snapshots import (
    MetadataSnapshotService,
)
from data_agent.ddl_metadata.worker.maintenance import (
    cleanup_checkpoints,
    dispatch_pending,
)
from data_agent.ddl_metadata.workflow.contracts import DDLGraphDependencies
from data_agent.ddl_metadata.workflow.graph import build_ddl_metadata_graph
from data_agent.ddl_metadata.workflow.llm_metadata_generator import (
    LLMMetadataGenerator,
)
from data_agent.ddl_metadata.workflow.memory_context import MemoryContextLoader
from data_agent.errors import DataAgentError
from data_agent.infrastructure.checkpoint_store import CheckpointStore
from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.llm_client import LLMClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.infrastructure.redis import RedisClient
from data_agent.infrastructure.tei_embeddings import TEIEmbeddingClient
from data_agent.logging import setup_logging
from data_agent.memory.indexing.elasticsearch import (
    MemoryElasticsearchIndex,
)
from data_agent.memory.indexing.qdrant import MemoryQdrantIndex
from data_agent.metadata_indexing.elasticsearch import (
    MetadataValueElasticsearchIndex,
)
from data_agent.metadata_indexing.qdrant import MetadataQdrantIndex
from data_agent.settings import app_config


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
    elasticsearch = ElasticsearchClient.initialize()
    qdrant = QdrantClient.initialize()
    TEIEmbeddingClient.initialize()
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
    # 步骤四：模型能力探测与 checkpoint 初始化成功后，才装配任务门面和工作流。
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
    ctx["conversation_extractor"] = ConversationMemoryExtractor(model)
    ctx["graph"] = build_ddl_metadata_graph(
        DDLGraphDependencies(
            model=LLMMetadataGenerator(),
            memory_context=MemoryContextLoader(),
            snapshot=MetadataSnapshotService(),
        ),
        checkpointer,
    )
    # 步骤五：服务就绪前先重放 dispatch 与 checkpoint 清理 outbox，再报告启动完成。
    await dispatch_pending(ctx)
    await cleanup_checkpoints(ctx)
    logger.info("DDL 元数据 worker 已启动，任务执行与周期维护资源均已就绪")


async def shutdown(ctx: dict[Any, Any]) -> None:
    """按依赖逆序关闭 worker 资源。"""
    # 步骤一：丢弃进程上下文引用，并按依赖逆序释放所有长生命周期资源。
    del ctx
    try:
        await CheckpointStore.close()
        await LLMClient.close()
        await TEIEmbeddingClient.close()
        await QdrantClient.close()
        await ElasticsearchClient.close()
        await MySQLDatabase.close()
        await RedisClient.close()
        logger.info("DDL 元数据 worker 已停止，进程内共享资源已经关闭")
    # 步骤二：无论资源关闭是否抛错，最后都刷新日志队列。
    finally:
        await logger.complete()
