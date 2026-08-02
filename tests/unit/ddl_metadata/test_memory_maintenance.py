"""Worker 的 Long-term Memory 生命周期注入 seam 测试。"""

from typing import Any

from data_agent.ddl_metadata.worker.maintenance import (
    dispatch_memory_index_outbox,
    expire_memories,
    purge_user_memories,
    report_memory_index_dead_letters,
)
from data_agent.memory.application.maintenance import MemoryMaintenance


class InMemoryMaintenanceStore:
    """记录到期与物理清理后的可观察状态。"""

    def __init__(self) -> None:
        """初始化计数。"""
        self.expired = 0
        self.purged = 0

    async def expire_due(self) -> int:
        """模拟一次到期处理。"""
        self.expired += 2
        return 2

    async def purge_ready_user_memories(self) -> int:
        """模拟一次安全物理清理。"""
        self.purged += 1
        return 1


class RecordingDispatcher:
    """记录 worker cron 对长生命周期 dispatcher 的使用。"""

    def __init__(self) -> None:
        """初始化调用状态。"""
        self.dispatched = False
        self.reported = False

    async def dispatch(self) -> int:
        """记录一次 dispatch。"""
        self.dispatched = True
        return 1

    async def report_dead_letters(self) -> int:
        """记录一次死信报告。"""
        self.reported = True
        return 0


async def test_memory_crons_use_worker_context_instances() -> None:
    """所有 Memory cron 只调用 startup 注入的长生命周期用例。"""
    store = InMemoryMaintenanceStore()
    maintenance = MemoryMaintenance(store)
    dispatcher = RecordingDispatcher()
    ctx: dict[Any, Any] = {
        "memory_dispatcher": dispatcher,
        "memory_maintenance": maintenance,
    }

    await dispatch_memory_index_outbox(ctx)
    await report_memory_index_dead_letters(ctx)
    await expire_memories(ctx)
    await purge_user_memories(ctx)

    assert dispatcher.dispatched is True
    assert dispatcher.reported is True
    assert store.expired == 2
    assert store.purged == 1
