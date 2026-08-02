"""Data Sync application 的生产适配器组合。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from data_agent.data_sync.adapters.mysql import (
    MySQLMaterializationAdapter,
    MySQLSyncTaskAdapter,
    RenewingLeaseCoordinator,
    ValueProjectionFactory,
)
from data_agent.data_sync.adapters.source import MySQLSourceAdapter
from data_agent.data_sync.application.contracts import SyncPolicy
from data_agent.data_sync.application.service import DataSyncService
from data_agent.data_sync.binlog import MySQLSourceClient
from data_agent.settings import DataSyncSettings


@dataclass(frozen=True, slots=True)
class DataSyncRuntime:
    """Worker 启动与调度所需的 Data Sync 组合结果。"""

    service: DataSyncService
    tasks: MySQLSyncTaskAdapter


def build_data_sync_runtime(
    clients: Mapping[str, MySQLSourceClient],
    settings: DataSyncSettings,
    *,
    projection_factory: ValueProjectionFactory,
) -> DataSyncRuntime:
    """选择 MySQL/source/projection adapters 并组合 application module。"""
    tasks = MySQLSyncTaskAdapter()
    materialization = MySQLMaterializationAdapter(settings, projection_factory)
    leases = RenewingLeaseCoordinator(
        tasks,
        lease_seconds=settings.claim_lease_seconds,
    )
    policy = SyncPolicy(
        claim_lease_seconds=settings.claim_lease_seconds,
        max_attempts=settings.max_attempts,
        event_cleanup_batch_size=settings.event_cleanup_batch_size,
        event_buffer_limit=settings.event_buffer_limit,
        backfill_batch_size=settings.backfill_batch_size,
        backfill_interval_seconds=settings.backfill_interval_seconds,
        poll_interval_seconds=settings.poll_interval_seconds,
        retry_base_seconds=settings.retry_base_seconds,
        retry_max_seconds=settings.retry_max_seconds,
    )
    return DataSyncRuntime(
        service=DataSyncService(
            tasks=tasks,
            sources={
                name: MySQLSourceAdapter(client) for name, client in clients.items()
            },
            materialization=materialization,
            leases=leases,
            policy=policy,
        ),
        tasks=tasks,
    )
