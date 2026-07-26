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


def is_fatal_index_error(error: BaseException) -> bool:
    """判定索引初始化异常是否必须阻断 worker 启动。

    暂时不可用（连接失败、服务未就绪）只延后派生投影，MySQL 权威数据与 outbox 仍可
    恢复，因此允许继续启动并在后续启动重试。结构不兼容不会自行痊愈：继续启动会让
    dispatcher 直接向被污染的索引写入并确认 outbox 行，把错误映射固化下来，中文检索
    静默降级且再无请求能纠正它，必须由运维显式重建索引。

    Args:
        error: 索引初始化抛出的异常。

    Returns:
        是否属于必须阻断启动的结构性故障。
    """
    return (
        isinstance(error, DataAgentError)
        and error.code == "memory_index_mapping_invalid"
    )


async def startup(ctx: dict[Any, Any]) -> None:
    """显式初始化 worker 的全部长生命周期依赖。"""
    # 步骤一：先配置日志并初始化任务、数据库及派生索引所需的外部客户端。
    setup_logging()
    redis = RedisClient.initialize()
    MySQLDatabase.initialize()
    elasticsearch = ElasticsearchClient.initialize()
    qdrant = QdrantClient.initialize()
    TEIEmbeddingClient.initialize()
    # 步骤二：区分"暂时不可用"与"结构不兼容"。前者只延后派生投影，MySQL 权威数据
    # 与 outbox 仍可恢复；后者不会自行痊愈——继续启动会让 dispatcher 直接向被污染的
    # 索引写入并确认 outbox 行，把错误映射固化下来，中文检索静默降级且再无请求能
    # 纠正它，因此必须让启动失败，由运维显式重建索引。
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
    # 步骤三：模型能力探测与 checkpoint 初始化成功后，才装配任务门面和工作流。
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
    # 步骤四：服务就绪前先重放 dispatch 与 checkpoint 清理 outbox，再报告启动完成。
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
