"""Meta 语义与字段值索引 outbox 调度。"""

from loguru import logger

from data_agent.data_sync.locks import generation_lock_name
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.infrastructure.tei_embeddings import TEIEmbeddingClient
from data_agent.metadata_indexing.models import (
    ClaimedMetadataIndexWork,
    MetadataIndexOperation,
    MetadataIndexTarget,
)
from data_agent.metadata_indexing.projections import (
    MetadataProjectionRepository,
    ProjectionNotReadyError,
)
from data_agent.metadata_indexing.qdrant import MetadataQdrantIndex
from data_agent.metadata_indexing.rebuilder import (
    MetadataIndexRebuilder,
    RebuildProjectionError,
)
from data_agent.metadata_indexing.repository import MetadataIndexOutboxRepository
from data_agent.metadata_indexing.value_refresh import (
    MetadataValueRefresh,
    ValueRefreshPersistenceError,
)
from data_agent.settings import app_config


class LocalProjectionError(RuntimeError):
    """权威 MySQL 投影读取失败，不应消耗远程服务失败预算。"""


class MetadataIndexDispatcher:
    """以短事务领取和结算可重建的 Meta 索引投影。"""

    async def dispatch(self) -> int:
        """有界处理一个 outbox 批次，外部调用期间不持有 MySQL 事务。"""
        # 步骤一：短事务领取任务并立即提交租约。
        async with MySQLDatabase.session() as session:
            items = await MetadataIndexOutboxRepository(session).claim()
        processed = 0
        # 步骤二：逐项隔离投影读取、外部写入与结算。
        for item in items:
            lock_scope = (
                "metadata-values"
                if item.target == MetadataIndexTarget.VALUES
                else f"metadata-semantic-{item.object_kind.value}"
            )
            lock = generation_lock_name(lock_scope, item.object_id)
            rebuild_lock = generation_lock_name("metadata-index-rebuild", "all")
            async with MySQLDatabase.advisory_locks(
                {rebuild_lock, lock},
                timeout_seconds=app_config.data_sync.generation_lock_timeout_seconds,
            ):
                # 等锁期间期望状态可能已被替换；过期 worker 不得触碰外部索引。
                async with MySQLDatabase.session() as session:
                    authoritative = await MetadataIndexOutboxRepository(
                        session
                    ).renew_lease(item)
                if authoritative and await self._synchronize(item):
                    processed += 1
        return processed

    async def report_dead_letters(self) -> None:
        """记录已达到最大失败次数的索引期望状态。"""
        # 步骤一：死信不再被领取，只通过安全计数暴露。
        async with MySQLDatabase.session() as session:
            count = await MetadataIndexOutboxRepository(session).dead_letter_count()
        if count:
            logger.warning(
                "Meta 索引期望状态已达最大失败次数并停止重试，待处理项数：{count}",
                count=count,
            )

    async def _synchronize(self, item: ClaimedMetadataIndexWork) -> bool:
        """处理一个目标，并按完整 desired identity 确认或退避。"""
        semantic_fingerprint: str | None = None
        try:
            if item.operation == MetadataIndexOperation.REBUILD:
                await MetadataIndexRebuilder().rebuild_target(item.target)
                semantic_fingerprint = None
            # 步骤一：只在短事务内读取权威投影，随后关闭事务再调用外部服务。
            elif item.target == MetadataIndexTarget.SEMANTIC:
                semantic_fingerprint = await self._synchronize_semantic(item)
            else:
                await self._synchronize_values(item)
                return True
        except ProjectionNotReadyError:
            async with MySQLDatabase.session() as session:
                await MetadataIndexOutboxRepository(session).defer(item)
            logger.info("Meta 字段值投影等待 DW 表完成物化")
            return False
        except (LocalProjectionError, RebuildProjectionError):
            async with MySQLDatabase.session() as session:
                await MetadataIndexOutboxRepository(session).defer(item)
            logger.warning("Meta 索引权威投影读取失败，本次处理无损延后")
            return False
        except Exception as error:
            # 步骤二：失败只退避本项，异常内容不进入持久化状态。
            async with MySQLDatabase.session() as session:
                await MetadataIndexOutboxRepository(session).backoff(
                    item,
                    type(error).__name__,
                )
            logger.warning("Meta 索引同步失败，当前项目已退避并等待自动重试")
            return False
        # 步骤三：语义写入先重读当前 Meta 指纹，再按 desired identity 结算。
        async with MySQLDatabase.session() as session:
            repository = MetadataIndexOutboxRepository(session)
            if (
                item.target == MetadataIndexTarget.SEMANTIC
                and item.operation != MetadataIndexOperation.REBUILD
            ):
                current = await MetadataProjectionRepository(
                    session
                ).semantic_projection(item.object_kind, item.object_id)
                current_fingerprint = (
                    current.schema_fingerprint if current is not None else None
                )
                acknowledged = (
                    current_fingerprint == semantic_fingerprint
                    and await repository.acknowledge(item)
                )
            else:
                acknowledged = await repository.acknowledge(item)
            if not acknowledged:
                await repository.restore_reconciliation(item)
        if not acknowledged:
            logger.warning(
                "Meta 索引期望状态已被更新，本次迟到写入不予确认并确保后续重新收敛"
            )
        return acknowledged

    async def _synchronize_semantic(
        self,
        item: ClaimedMetadataIndexWork,
    ) -> str | None:
        """把一个 Meta 对象收敛到 Qdrant 当前权威状态并返回写入指纹。"""
        index = MetadataQdrantIndex(QdrantClient.get_client())
        if item.operation == MetadataIndexOperation.DELETE:
            await index.delete(item.object_kind, item.object_id)
            return None
        try:
            async with MySQLDatabase.session() as session:
                projection = await MetadataProjectionRepository(
                    session
                ).semantic_projection(
                    item.object_kind,
                    item.object_id,
                )
        except Exception as error:
            raise LocalProjectionError from error
        if projection is None:
            await index.delete(item.object_kind, item.object_id)
            return None
        vectors = await TEIEmbeddingClient.get_client().aembed_documents(
            [projection.search_text]
        )
        await index.upsert(projection, vectors[0])
        return projection.schema_fingerprint

    async def _synchronize_values(
        self,
        item: ClaimedMetadataIndexWork,
    ) -> None:
        """委托深 module 执行一个有界、可恢复的 VALUES 工作单元。"""
        if item.operation != MetadataIndexOperation.REFRESH:
            raise ValueError("字段值索引仅支持 refresh 期望状态")
        try:
            await MetadataValueRefresh().run_next_unit(item)
        except ProjectionNotReadyError:
            raise
        except ValueRefreshPersistenceError as error:
            raise LocalProjectionError from error
