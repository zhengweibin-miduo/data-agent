"""Long-term Memory 可重建投影调度用例。"""

from collections.abc import Mapping

from loguru import logger

from memory.application.contracts import (
    MemoryProjectionDispatchConfig,
    MemoryProjectionIndex,
    MemoryProjectionWorkStore,
)
from models.memory import (
    MemoryIndexOperation,
    MemoryIndexTarget,
    MemoryOutboxItem,
    MemoryProjection,
    MemoryStatus,
)


class MemoryIndexDispatcher:
    """通过注入端口编排投影期望状态的领取、远程写入与结算。"""

    def __init__(
        self,
        work: MemoryProjectionWorkStore,
        indexes: Mapping[MemoryIndexTarget, MemoryProjectionIndex],
        config: MemoryProjectionDispatchConfig,
    ) -> None:
        """绑定持久 work store、目标适配器与显式调度预算。"""
        self._work = work
        self._indexes = indexes
        self._config = config

    async def dispatch(self) -> int:
        """有界处理一个批次，并让各投影目标独立收敛。"""
        # 步骤一：短事务领取后逐项处理，远程调用不跨越 work store 的方法边界。
        items = await self._work.claim(self._config.batch_size)
        processed = 0
        for item in items:
            if await self._synchronize(item):
                processed += 1
        return processed

    async def report_dead_letters(self) -> int:
        """记录并返回已停止重试的投影期望状态数量。"""
        # 步骤一：死信不再参与领取，必须通过独立周期显式暴露。
        count = await self._work.dead_letter_count()
        if count:
            logger.warning(
                "记忆索引期望状态已达最大失败次数并停止重试，待人工处理项数：{count}",
                count=count,
            )
        return count

    async def _synchronize(self, item: MemoryOutboxItem) -> bool:
        """处理一个目标的 authority 复核、远程应用与短事务结算。"""
        try:
            # 步骤一：续租和权威读取由 adapter 在同一短事务内完成。
            prepared = await self._work.prepare(item)
            if not prepared.authority_held:
                logger.warning("记忆索引领取代次已失效，本项目交由重新领取者处理")
                return False
            writable = self._writable_projection(item, prepared.projection)
            # 步骤二：远程调用发生在 work store 事务之外，并按目标独立失败。
            await self._indexes[item.target].apply(item.memory_uid, writable)
        except Exception as error:
            # 步骤三：单项失败只消费自身重试预算，不阻断同批其他目标。
            await self._work.settle_failure(
                item,
                error_type=type(error).__name__,
                max_backoff_seconds=self._config.max_backoff_seconds,
            )
            logger.warning("记忆索引同步失败，当前项目已进入退避并等待自动重试")
            return False
        # 步骤四：结算复核实际写入的内容哈希；authority 变化由 adapter 原子登记
        # 可重放收敛请求，迟到写入不会确认或只做一次性补偿。
        acknowledged = await self._work.settle_success(
            item,
            content_hash=(writable.content_hash if writable is not None else None),
        )
        if not acknowledged:
            logger.warning("权威内容在派生索引写入期间已变更，本次同步不确认并等待重新处理")
        return acknowledged

    @staticmethod
    def _writable_projection(
        item: MemoryOutboxItem,
        projection: MemoryProjection | None,
    ) -> MemoryProjection | None:
        """把删除、缺失或非活动 authority 统一裁决为派生删除。"""
        if item.operation == MemoryIndexOperation.DELETE:
            return None
        if projection is None or projection.status != MemoryStatus.ACTIVE:
            return None
        return projection
