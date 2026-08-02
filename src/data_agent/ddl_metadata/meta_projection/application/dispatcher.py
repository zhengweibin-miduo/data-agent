"""Meta Projection claim-remote-settle 调度用例。"""

from __future__ import annotations

from loguru import logger

from data_agent.ddl_metadata.meta_projection.application.contracts import (
    ProjectionNotReadyError,
    ProjectionReader,
    ProjectionRebuilder,
    ProjectionWorkStore,
    RebuildProjectionError,
    SemanticIndex,
    ValueRefreshPersistenceError,
    ValueRefreshRunner,
)
from data_agent.ddl_metadata.meta_projection.models import (
    ClaimedMetadataIndexWork,
    MetadataIndexOperation,
    MetadataIndexTarget,
)


class LocalProjectionError(RuntimeError):
    """权威 MySQL 投影读取失败，不消耗远程服务失败预算。"""


class MetadataIndexDispatcher:
    """通过注入端口有界收敛 Meta 语义与字段值投影。"""

    def __init__(
        self,
        *,
        work_store: ProjectionWorkStore,
        reader: ProjectionReader,
        semantic_index: SemanticIndex,
        value_refresh: ValueRefreshRunner,
        rebuilder: ProjectionRebuilder,
    ) -> None:
        """绑定运行期端口，不在用例内部构造 concrete clients。"""
        self._work_store = work_store
        self._reader = reader
        self._semantic_index = semantic_index
        self._value_refresh = value_refresh
        self._rebuilder = rebuilder

    async def dispatch(self, limit: int = 100) -> int:
        """有界执行 claim-remote-settle，取消异常原样传播。"""
        if limit <= 0:
            raise ValueError("投影 dispatch limit 必须为正整数")
        # 步骤一：通过短事务端口领取工作；外部调用不持有数据库事务。
        items = await self._work_store.claim(limit)
        processed = 0
        for item in items:
            # 步骤二：外层 adapter 持有投影锁并重新确认完整 desired identity。
            async with self._work_store.authority(item) as authoritative:
                if authoritative and await self._synchronize(item):
                    processed += 1
        return processed

    async def report_dead_letters(self) -> None:
        """记录已达到最大远程失败次数的 desired state。"""
        count = await self._work_store.dead_letter_count()
        if count:
            logger.warning(
                "Meta 索引期望状态已达最大失败次数并停止重试，待处理项数：{count}",
                count=count,
            )

    async def _synchronize(self, item: ClaimedMetadataIndexWork) -> bool:
        """完成一项远程工作并通过持久化端口结算。"""
        written_fingerprint: str | None = None
        try:
            if item.operation == MetadataIndexOperation.REBUILD:
                await self._rebuilder.rebuild_target(item.target)
            elif item.target == MetadataIndexTarget.SEMANTIC:
                written_fingerprint = await self._synchronize_semantic(item)
            else:
                await self._synchronize_values(item)
                return True
        except (ProjectionNotReadyError, LocalProjectionError, RebuildProjectionError):
            await self._work_store.defer(item)
            logger.warning("Meta 索引权威投影读取失败，本次处理无损延后")
            return False
        except Exception as error:
            await self._work_store.backoff(item, type(error).__name__)
            logger.warning("Meta 索引同步失败，当前项目已退避并等待自动重试")
            return False

        # 步骤三：语义远程写入后重读权威指纹，迟到写入不得确认新 desired state。
        if (
            item.target == MetadataIndexTarget.SEMANTIC
            and item.operation != MetadataIndexOperation.REBUILD
        ):
            current = await self._reader.semantic_projection(
                item.object_kind, item.object_id
            )
            current_fingerprint = (
                current.schema_fingerprint if current is not None else None
            )
            acknowledged = (
                current_fingerprint == written_fingerprint
                and await self._work_store.acknowledge(item)
            )
        else:
            acknowledged = await self._work_store.acknowledge(item)
        if not acknowledged:
            await self._work_store.restore_reconciliation(item)
            logger.warning(
                "Meta 索引期望状态已被更新，本次迟到写入不予确认并确保后续重新收敛"
            )
        return acknowledged

    async def _synchronize_semantic(
        self,
        item: ClaimedMetadataIndexWork,
    ) -> str | None:
        """把一个语义对象收敛到当前权威投影。"""
        if item.operation == MetadataIndexOperation.DELETE:
            await self._semantic_index.delete(item.object_kind, item.object_id)
            return None
        try:
            projection = await self._reader.semantic_projection(
                item.object_kind, item.object_id
            )
        except Exception as error:
            raise LocalProjectionError from error
        if projection is None:
            await self._semantic_index.delete(item.object_kind, item.object_id)
            return None
        await self._semantic_index.upsert(projection)
        return projection.schema_fingerprint

    async def _synchronize_values(self, item: ClaimedMetadataIndexWork) -> None:
        """委托注入的刷新端口推进一个有界 VALUES 工作单元。"""
        if item.operation != MetadataIndexOperation.REFRESH:
            raise ValueError("字段值索引仅支持 refresh 期望状态")
        try:
            await self._value_refresh.run_next_unit(item)
        except ProjectionNotReadyError:
            raise
        except ValueRefreshPersistenceError as error:
            raise LocalProjectionError from error
