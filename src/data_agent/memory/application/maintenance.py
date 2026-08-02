"""Long-term Memory 到期与安全物理清理用例。"""

from data_agent.memory.application.contracts import MemoryMaintenanceStore


class MemoryMaintenance:
    """通过注入 store 执行 Long-term Memory 生命周期维护。"""

    def __init__(self, store: MemoryMaintenanceStore) -> None:
        """绑定权威维护事务端口。"""
        self._store = store

    async def expire_due(self) -> int:
        """失效到期权威记忆并返回处理数量。"""
        return await self._store.expire_due()

    async def purge_ready_user_memories(self) -> int:
        """物理清理投影删除已收敛的用户记忆并返回数量。"""
        return await self._store.purge_ready_user_memories()
