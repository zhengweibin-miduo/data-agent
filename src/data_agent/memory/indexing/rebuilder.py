"""记忆派生索引重建。"""

from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.memory.indexing.elasticsearch import (
    MemoryElasticsearchIndex,
)
from data_agent.memory.indexing.qdrant import MemoryQdrantIndex
from data_agent.memory.mysql.index_outbox import (
    MemoryIndexOutboxRepository,
)
from data_agent.models.memory import MemoryRebuildResult
from data_agent.settings import app_config


class MemoryIndexRebuilder:
    """从 MySQL 权威记忆重建项目专用派生索引。"""

    async def reset_indexes(
        self,
        *,
        confirmed_es_index: str,
        confirmed_qdrant_collection: str,
    ) -> None:
        """仅在调用方逐字确认两个配置目标后重建派生索引。"""
        expected = (
            app_config.elasticsearch.memory_index,
            app_config.qdrant.memory_collection,
        )
        if (confirmed_es_index, confirmed_qdrant_collection) != expected:
            raise ValueError(
                "索引重建目标确认不匹配: "
                f"Elasticsearch={expected[0]}, Qdrant={expected[1]}"
            )
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
