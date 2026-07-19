"""记忆索引 outbox 调度。"""

from loguru import logger

from data_agent.ddl_metadata.memory.indexing.elasticsearch import (
    MemoryElasticsearchIndex,
)
from data_agent.ddl_metadata.memory.indexing.qdrant import MemoryQdrantIndex
from data_agent.ddl_metadata.memory.mysql.index_outbox import (
    MemoryIndexOutboxRepository,
)
from data_agent.ddl_metadata.models.memory import (
    MemoryIndexOperation,
    MemoryIndexTarget,
)
from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.infrastructure.tei_embeddings import TEIEmbeddingClient
from data_agent.settings import app_config


class MemoryIndexDispatcher:
    """独立确认 ES/Qdrant 投影期望状态。"""

    async def dispatch(self) -> int:
        """有界领取并处理一个 outbox 批次。"""
        processed = 0
        async with MySQLDatabase.session() as session:
            repository = MemoryIndexOutboxRepository(session)
            items = await repository.claim_outbox(app_config.memory.outbox_batch_size)
            for item in items:
                try:
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
                    await repository.acknowledge_outbox(item)
                    processed += 1
                except Exception as error:
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
