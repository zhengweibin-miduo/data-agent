"""记忆派生索引重建。"""

from infrastructure.elasticsearch import ElasticsearchClient
from infrastructure.mysql import MySQLDatabase
from infrastructure.qdrant import QdrantClient
from memory.indexing.elasticsearch import (
    MemoryElasticsearchIndex,
)
from memory.indexing.qdrant import MemoryQdrantIndex
from memory.mysql.index_outbox import (
    MemoryIndexOutboxRepository,
)
from models.memory import MemoryRebuildResult
from settings import app_config


class MemoryIndexRebuilder:
    """从 MySQL 权威记忆重建项目专用派生索引。"""

    async def reset_indexes(
        self,
        *,
        confirmed_es_index: str,
        confirmed_qdrant_collection: str,
    ) -> None:
        """仅在调用方逐字确认两个配置目标后重建派生索引。"""
        # 步骤一：逐字校验两个目标，防止误删非项目资源。
        expected = (
            app_config.elasticsearch.memory_index,
            app_config.qdrant.memory_collection,
        )
        if (confirmed_es_index, confirmed_qdrant_collection) != expected:
            raise ValueError(
                "索引重建目标确认不匹配: "
                f"Elasticsearch={expected[0]}, Qdrant={expected[1]}"
            )
        # 步骤二：确认后只重建配置中的 ES 索引和 Qdrant 集合。
        await MemoryElasticsearchIndex(ElasticsearchClient.get_client()).recreate()
        await MemoryQdrantIndex(QdrantClient.get_client()).recreate()

    async def enqueue_batch(self, after_id: int = 0) -> MemoryRebuildResult:
        """按权威主键游标生成一个双目标 UPSERT 批次。"""
        # 步骤一：按权威主键游标有界扫描活动记忆。
        async with MySQLDatabase.session() as session:
            repository = MemoryIndexOutboxRepository(session)
            rows = await repository.scan_active(
                after_id=after_id,
                limit=app_config.memory.rebuild_batch_size,
            )
            # 步骤二：为本批 UID 写入两个派生目标的 UPSERT 期望状态。
            await repository.enqueue_rebuild({str(row["uid"]) for row in rows})
        # 步骤三：仅在本批已满时返回下一游标，空余批次表示扫描结束。
        return MemoryRebuildResult(
            processed=len(rows),
            next_after_id=(
                int(rows[-1]["id"])
                if len(rows) == app_config.memory.rebuild_batch_size
                else None
            ),
        )
