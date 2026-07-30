"""权威元数据与记忆快照的原子提交。"""

from collections.abc import Mapping

from loguru import logger
from sqlalchemy.engine import make_url

from data_agent.data_sync.locks import generation_lock_name
from data_agent.data_sync.models import build_desired_tables
from data_agent.data_sync.repository import DataSyncRepository
from data_agent.ddl_metadata.persistence.metadata_repository import MetadataRepository
from data_agent.errors import DataAgentError
from data_agent.identifiers import scope_fingerprint
from data_agent.infrastructure.mysql import (
    AdvisoryLockReleaseError,
    AdvisoryLockUnavailableError,
    MySQLDatabase,
)
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
        generation_locks = {
            generation_lock_name(app_config.data_sync.dw_database, item.target_table)
            for item in desired_tables
        }
        try:
            # 步骤四：先持有本次全部 target generation locks，再开启唯一发布事务。
            async with MySQLDatabase.advisory_locks(
                generation_locks,
                timeout_seconds=(
                    app_config.data_sync.generation_lock_timeout_seconds
                ),
            ):
                # 步骤五：事务提交或回滚完成后，外层上下文才会释放 generation locks。
                async with MySQLDatabase.session() as session:
                    metadata_repository = MetadataRepository(session)
                    # 步骤六：计算本次提交范围内可能受结构变化影响的记忆键。
                    expiration_memory_keys = (
                        await metadata_repository.fingerprint_expiration_memory_keys(
                            schema,
                            metrics,
                        )
                    )
                    memory_repository = MemoryRepository(session)
                    # 步骤七：在写入新快照前过期指纹不再兼容的权威记忆版本。
                    await memory_repository.expire_fingerprint_bound(
                        schema.source,
                        valid_fingerprints,
                        memory_keys=expiration_memory_keys,
                    )
                    # 步骤八：同步严格受本次提交表约束的 Meta 快照及其关联清理。
                    await metadata_repository.synchronize(schema, metadata, metrics)
                    # 步骤九：在同一事务发布 durable generation handoff。
                    sync_repository = DataSyncRepository(session)
                    await sync_repository.upsert_desired(desired_tables)
                    # 步骤十：最后写入权威记忆、审计事件、关系和双索引 outbox。
                    await memory_repository.upsert_candidates(accepted)
        except AdvisoryLockUnavailableError as error:
            raise DataAgentError(
                "generation_lock_unavailable",
                "persist_snapshot",
                "DW generation 正在变更，accepted snapshot 稍后可安全重试",
                retryable=True,
                http_status=503,
            ) from error
        except AdvisoryLockReleaseError:
            # 业务 Session 已在 advisory lock 上下文退出前提交；owner 连接也已
            # 失效。此时锁清理故障只能降级为运维告警，不能反转权威快照结果。
            logger.warning(
                "accepted snapshot 已提交，但 generation lock owner 连接"
                "释放失败且已失效"
            )
