"""权威元数据与记忆快照的原子提交。"""

from data_agent.ddl_metadata.memory.domain.candidates import build_accepted_memories
from data_agent.ddl_metadata.memory.mysql.repository import MemoryRepository
from data_agent.ddl_metadata.models.memory import MemoryCandidate
from data_agent.ddl_metadata.models.physical import PhysicalSchema
from data_agent.ddl_metadata.models.semantic import (
    MetricAnswer,
    MetricMetadata,
    MetricQuestion,
    SemanticMetadata,
)
from data_agent.ddl_metadata.persistence.metadata_repository import MetadataRepository
from data_agent.infrastructure.mysql import MySQLDatabase


class MetadataSnapshotService:
    """在一个 MySQL 事务内提交 Meta、权威记忆与双索引 outbox。"""

    async def persist(
        self,
        schema: PhysicalSchema,
        metadata: SemanticMetadata,
        questions: list[MetricQuestion],
        answers: list[MetricAnswer],
        metrics: list[MetricMetadata],
        candidates: list[MemoryCandidate] | None = None,
    ) -> None:
        """提交最终通过确定性校验的完整快照。"""
        accepted = candidates or build_accepted_memories(
            schema,
            metadata,
            questions,
            answers,
            metrics,
        )
        async with MySQLDatabase.session() as session:
            await MetadataRepository(session).synchronize(schema, metadata, metrics)
            await MemoryRepository(session).upsert_candidates(accepted)
