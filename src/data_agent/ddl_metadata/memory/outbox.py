"""索引 outbox 调度与可重建投影服务。"""

from loguru import logger

from data_agent.ddl_metadata.memory.indexes import (
    MemoryElasticsearchIndex,
    MemoryQdrantIndex,
)
from data_agent.ddl_metadata.models import (
    MemoryIndexOperation,
    MemoryIndexTarget,
    MemoryRebuildResult,
)
from data_agent.ddl_metadata.persistence.memory_repository import MemoryRepository
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
            repository = MemoryRepository(session)
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


class MemoryIndexRebuilder:
    """从 MySQL 权威记忆重建项目专用派生索引。"""

    async def reset_indexes(self) -> None:
        """显式清空并重建配置的 ES index 与 Qdrant collection。"""
        await MemoryElasticsearchIndex(ElasticsearchClient.get_client()).recreate()
        await MemoryQdrantIndex(QdrantClient.get_client()).recreate()

    async def enqueue_batch(self, after_id: int = 0) -> MemoryRebuildResult:
        """按权威主键游标生成一个双目标 UPSERT 批次。"""
        async with MySQLDatabase.session() as session:
            repository = MemoryRepository(session)
            rows = await repository.scan_active(
                after_id=after_id,
                limit=app_config.memory.rebuild_batch_size,
            )
            await repository.enqueue_rebuild({str(row["uid"]) for row in rows})
        return MemoryRebuildResult(
            processed=len(rows),
            next_after_id=(
                int(rows[-1]["id"])
                if len(rows) == app_config.memory.rebuild_batch_size
                else None
            ),
        )
