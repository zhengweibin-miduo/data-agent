"""DDL 元数据 worker 资源生命周期与图装配。"""

from typing import Any

from loguru import logger

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


async def startup(ctx: dict[Any, Any]) -> None:
    """显式初始化 worker 的全部长生命周期依赖。"""
    # 步骤一：先配置日志并初始化任务、数据库及派生索引所需的外部客户端。
    setup_logging()
    redis = RedisClient.initialize()
    MySQLDatabase.initialize()
    elasticsearch = ElasticsearchClient.initialize()
    qdrant = QdrantClient.initialize()
    TEIEmbeddingClient.initialize()
    # 步骤二：索引初始化失败只延后派生投影，MySQL 权威数据和 outbox 仍可恢复。
    for target, setup in (
        ("ELASTICSEARCH", MemoryElasticsearchIndex(elasticsearch).setup),
        ("QDRANT", MemoryQdrantIndex(qdrant).setup),
    ):
        try:
            await setup()
        except Exception as error:
            logger.bind(
                trace_id="-",
                component="ddl_metadata.worker",
                event_name="ddl_metadata.memory.index_initialization_deferred",
                operation="setup_memory_index",
                outcome="deferred",
                stage=target.lower(),
                error_type=type(error).__name__,
                retryable=True,
            ).warning("记忆索引初始化延后")
    # 步骤三：模型能力探测与 checkpoint 初始化成功后，才装配任务门面和工作流。
    model = LLMClient.initialize()
    await LLMClient.check_structured_output_capability()
    checkpointer = await CheckpointStore.initialize()
    ctx["jobs"] = DDLJobStore(redis)
    ctx["conversation_extractor"] = ConversationMemoryExtractor(model)
    ctx["graph"] = build_ddl_metadata_graph(
        DDLGraphDependencies(
            model=LLMMetadataGenerator(),
            memory_context=MemoryContextLoader(),
            snapshot=MetadataSnapshotService(),
        ),
        checkpointer,
    )
    # 步骤四：服务就绪前先重放 dispatch 与 checkpoint 清理 outbox，再报告启动完成。
    await dispatch_pending(ctx)
    await cleanup_checkpoints(ctx)
    logger.bind(
        component="application.worker",
        event_name="application.lifecycle.started",
        operation="run_worker",
        outcome="started",
        worker_role="ddl_metadata",
    ).info("DDL 元数据 worker 已启动")


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
        logger.bind(
            component="application.worker",
            event_name="application.lifecycle.stopped",
            operation="run_worker",
            outcome="stopped",
            worker_role="ddl_metadata",
        ).info("DDL 元数据 worker 已停止")
    # 步骤二：无论资源关闭是否抛错，最后都刷新日志队列。
    finally:
        await logger.complete()
