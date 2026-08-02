"""DDL worker 周期维护任务的运行时装配测试。"""

from dataclasses import dataclass

from data_agent.ddl_metadata.worker.maintenance import (
    dispatch_metadata_index_outbox,
    report_metadata_index_dead_letters,
)


@dataclass
class _MetadataIndexDispatcher:
    """记录 maintenance 是否复用 lifecycle 注入的 dispatcher。"""

    dispatched: int = 0
    reported: int = 0

    async def dispatch(self) -> int:
        """记录一次投影调度。"""
        self.dispatched += 1
        return 1

    async def report_dead_letters(self) -> None:
        """记录一次死信巡检。"""
        self.reported += 1


async def test_metadata_projection_maintenance_reuses_lifecycle_dispatcher() -> None:
    """两个 Meta Projection cron 共用 ctx 中的长生命周期 dispatcher。"""
    dispatcher = _MetadataIndexDispatcher()
    ctx = {"metadata_index_dispatcher": dispatcher}

    await dispatch_metadata_index_outbox(ctx)
    await report_metadata_index_dead_letters(ctx)

    assert dispatcher.dispatched == 1
    assert dispatcher.reported == 1
