"""记忆索引 outbox 调度。"""

from loguru import logger

from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.infrastructure.tei_embeddings import TEIEmbeddingClient
from data_agent.memory.indexing.elasticsearch import (
    MemoryElasticsearchIndex,
)
from data_agent.memory.indexing.qdrant import MemoryQdrantIndex
from data_agent.memory.mysql.index_outbox import (
    MemoryIndexOutboxRepository,
)
from data_agent.models.memory import (
    MemoryIndexOperation,
    MemoryIndexTarget,
)
from data_agent.settings import app_config


class MemoryIndexDispatcher:
    """独立确认 ES/Qdrant 投影期望状态。"""

    async def dispatch(self) -> int:
        """有界领取并处理一个 outbox 批次。"""
        processed = 0
        # 步骤一：在当前事务中按配置上限领取待同步的期望状态。
        async with MySQLDatabase.session() as session:
            repository = MemoryIndexOutboxRepository(session)
            items = await repository.claim_outbox(app_config.memory.outbox_batch_size)
            # 步骤二：逐项处理 ES/Qdrant；两个目标独立确认，互不替代。
            for item in items:
                try:
                    # 步骤三：从 MySQL 重建当前投影，再执行目标对应的幂等写入。
                    projection = await repository.projection(item.memory_uid)
                    if item.target == MemoryIndexTarget.ELASTICSEARCH:
                        index = MemoryElasticsearchIndex(
                            ElasticsearchClient.get_client()
                        )
                        if item.operation == MemoryIndexOperation.DELETE:
                            await index.delete(item.memory_uid)
                        elif projection is not None:
                            await index.upsert(projection)
                    else:
                        index = MemoryQdrantIndex(QdrantClient.get_client())
                        if item.operation == MemoryIndexOperation.DELETE:
                            await index.delete(item.memory_uid)
                        elif projection is not None:
                            embeddings = TEIEmbeddingClient.get_client()
                            vector = await embeddings.aembed_documents(
                                [projection.memory_text]
                            )
                            await index.upsert(projection, vector[0])
                    # 步骤四：仅在外部写入成功后确认当前目标。
                    await repository.acknowledge_outbox(item)
                    processed += 1
                except Exception as error:
                    # 步骤五：单目标失败只退避自身行，保留期望状态供后续重试。
                    await repository.retry_outbox(
                        item,
                        type(error).__name__,
                        app_config.memory.outbox_max_backoff_seconds,
                    )
                    logger.bind(trace_id="-").warning(
                        "记忆索引同步延后 target={} error_type={}",
                        item.target.value,
                        type(error).__name__,
                    )
        return processed
