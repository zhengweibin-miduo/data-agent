"""权威元数据与记忆快照的原子提交。"""

from collections.abc import Mapping

from sqlalchemy import text
from sqlalchemy.engine import make_url

from data_agent.data_sync.models import build_desired_tables
from data_agent.data_sync.repository import DataSyncRepository
from data_agent.ddl_metadata.persistence.metadata_repository import MetadataRepository
from data_agent.errors import DataAgentError
from data_agent.identifiers import scope_fingerprint
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.memory.domain.candidates import MemoryVersions, build_accepted_memories
from data_agent.memory.mysql.repository import MemoryRepository
from data_agent.models.memory import MemoryCandidate
from data_agent.models.physical import PhysicalSchema
from data_agent.models.semantic import (
    MetricAnswer,
    MetricMetadata,
    MetricQuestion,
    SemanticMetadata,
)
from data_agent.settings import app_config


class MetadataSnapshotService:
    """在一个 MySQL 事务内提交 Meta、权威记忆与双索引 outbox。"""

    def __init__(self, source_schemas: Mapping[str, str] | None = None) -> None:
        """保存命名数据源到默认源库的可测试投影。"""
        self._source_schemas = (
            dict(source_schemas)
            if source_schemas is not None
            else {
                name: database
                for name, source in app_config.data_sync.sources.items()
                if (database := make_url(source.url).database) is not None
            }
        )

    async def persist(
        self,
        schema: PhysicalSchema,
        metadata: SemanticMetadata,
        questions: list[MetricQuestion],
        answers: list[MetricAnswer],
        metrics: list[MetricMetadata],
        candidates: list[MemoryCandidate] | None = None,
    ) -> None:
        """在单一事务中提交验证通过的 Meta 与权威记忆快照。

        先计算本次提交仍有效的对象作用域指纹，再过期不兼容记忆、同步
        Meta，最后写入权威记忆及其 ES/Qdrant desired-state outbox；任一步
        失败都由同一 managed Session 回滚。
        """
        # 步骤一：优先使用上游已验证候选，否则从最终快照构建同版本权威候选。
        accepted = candidates or build_accepted_memories(
            schema,
            metadata,
            questions,
            answers,
            metrics,
            versions=MemoryVersions(
                content=app_config.memory.content_version,
                projection=app_config.memory.projection_version,
            ),
        )
        # 步骤二：计算本次表和列仍有效的作用域指纹，并保留整体 schema 指纹。
        fingerprints = {
            object_id: scope_fingerprint(schema, object_id)
            for object_id in (
                *[table.id for table in schema.tables],
                *[column.id for table in schema.tables for column in table.columns],
            )
        }
        valid_fingerprints = set(fingerprints.values()) | {schema.schema_fingerprint}
        # 步骤三：从服务端命名源解析默认库，不把连接地址带入快照或日志。
        source_schema = self._source_schemas.get(schema.source)
        if source_schema is None:
            raise DataAgentError(
                "unknown_data_source",
                "persist_snapshot",
                "DDL source 未配置对应的 MySQL 数据源",
                details={"source": schema.source},
            )
        desired_tables = build_desired_tables(
            schema,
            metadata,
            metrics,
            default_source_schema=source_schema,
        )
        # 步骤四：进入唯一 managed Session，使 Meta、记忆、索引 outbox 与同步
        # 期望状态共享同一提交或回滚边界。
        lock_names = sorted(
            {
                (
                    "data_sync_schema:"
                    f"{app_config.data_sync.dw_database}:{item.target_table}"
                )[:64]
                for item in desired_tables
            }
        )
        # Named locks must stay on one physical connection through commit and be
        # released from that same connection on every success and failure path.
        async with MySQLDatabase.get_client().connect() as lock_connection:
            acquired: list[str] = []
            try:
                for lock_name in lock_names:
                    result = await lock_connection.scalar(
                        text("SELECT GET_LOCK(:lock_name, 10)"),
                        {"lock_name": lock_name},
                    )
                    if result != 1:
                        raise RuntimeError("无法取得 DW 结构 generation 串行锁")
                    acquired.append(lock_name)
                async with MySQLDatabase.session() as session:
                    metadata_repository = MetadataRepository(session)
                    # 步骤五：先计算本次提交范围内可能受结构变化影响的记忆键。
                    expiration_memory_keys = (
                        await metadata_repository.fingerprint_expiration_memory_keys(
                            schema,
                            metrics,
                        )
                    )
                    memory_repository = MemoryRepository(session)
                    # 步骤六：在写入新快照前过期指纹不再兼容的权威记忆版本。
                    await memory_repository.expire_fingerprint_bound(
                        schema.source,
                        valid_fingerprints,
                        memory_keys=expiration_memory_keys,
                    )
                    # 步骤七：同步严格受本次提交表约束的 Meta 快照及其关联清理。
                    await metadata_repository.synchronize(schema, metadata, metrics)
                    # 步骤八：写入同事务 durable handoff，不在图内连接 DW 或源库。
                    sync_repository = DataSyncRepository(session)
                    await sync_repository.upsert_desired(desired_tables)
                    # 步骤九：最后写入权威记忆、审计事件、关系和双索引 outbox。
                    await memory_repository.upsert_candidates(accepted)
                    # Commit happens on normal context exit while locks remain held.
            finally:
                for lock_name in reversed(acquired):
                    released = await lock_connection.scalar(
                        text("SELECT RELEASE_LOCK(:lock_name)"),
                        {"lock_name": lock_name},
                    )
                    if released != 1:
                        raise RuntimeError("释放 DW 结构 generation 串行锁失败")
