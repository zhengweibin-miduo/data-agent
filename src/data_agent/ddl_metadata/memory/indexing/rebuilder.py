"""记忆派生索引重建。"""

from data_agent.ddl_metadata.memory.indexing.elasticsearch import (
    MemoryElasticsearchIndex,
)
from data_agent.ddl_metadata.memory.indexing.qdrant import MemoryQdrantIndex
from data_agent.ddl_metadata.memory.mysql.index_outbox import (
    MemoryIndexOutboxRepository,
)
from data_agent.ddl_metadata.models.memory import MemoryRebuildResult
from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.settings import app_config


class MemoryIndexRebuilder:
    """从 MySQL 权威记忆重建项目专用派生索引。"""

    async def reset_indexes(self) -> None:
        """显式清空并重建配置的 ES index 与 Qdrant collection。"""
        await MemoryElasticsearchIndex(ElasticsearchClient.get_client()).recreate()
        await MemoryQdrantIndex(QdrantClient.get_client()).recreate()

    async def enqueue_batch(self, after_id: int = 0) -> MemoryRebuildResult:
        """按权威主键游标生成一个双目标 UPSERT 批次。"""
        async with MySQLDatabase.session() as session:
            repository = MemoryIndexOutboxRepository(session)
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
