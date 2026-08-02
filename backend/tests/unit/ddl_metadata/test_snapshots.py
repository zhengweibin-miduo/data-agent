"""Accepted snapshot generation 串行锁边界检查。"""

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from data_sync.locks import generation_lock_name
from ddl_metadata.adapters.mysql import accepted_snapshot as snapshots
from ddl_metadata.adapters.mysql.accepted_snapshot import (
    MySQLAcceptedSnapshotPublisher,
)
from ddl_metadata.application.accepted_snapshot import AcceptedSnapshot
from errors import DataAgentError
from models.physical import PhysicalSchema
from models.semantic import SemanticMetadata
from tests.helpers.checks import check_equal, check_exception


def _snapshot() -> PhysicalSchema:
    """构造只包含持久化编排所需字段的快照。"""
    return PhysicalSchema(
        source="local",
        canonical_ddl="",
        ddl_hash="d" * 64,
        tables=[],
        schema_fingerprint="f" * 64,
    )


def _metadata() -> SemanticMetadata:
    """构造不影响编排断言的空语义快照。"""
    return SemanticMetadata(tables=[], columns=[])


def _accepted_snapshot() -> AcceptedSnapshot:
    """构造公开发布 seam 使用的空 accepted snapshot。"""
    return AcceptedSnapshot(
        schema=_snapshot(),
        metadata=_metadata(),
        questions=(),
        answers=(),
        metrics=(),
        candidates=(),
    )


def _install_repository_fakes(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    fail_memory_write: BaseException | None = None,
) -> None:
    """安装只记录跨仓储调用顺序的轻量替身。"""

    class FakeMetadataRepository:
        """记录 Meta 快照读写。"""

        def __init__(self, session: object) -> None:
            del session

        async def fingerprint_expiration_memory_keys(
            self,
            schema: object,
            metrics: object,
        ) -> set[str]:
            del schema, metrics
            events.append("fingerprints")
            return set()

        async def semantic_scope_before_sync(
            self,
            schema: object,
        ) -> tuple[set[str], set[str]]:
            del schema
            return set(), set()

        async def existing_object_ids(
            self,
            table_ids: set[str],
            column_ids: set[str],
            metric_ids: set[str],
        ) -> set[str]:
            del table_ids, column_ids
            return metric_ids

        async def synchronize(
            self,
            schema: object,
            metadata: object,
            metrics: object,
        ) -> None:
            del schema, metadata, metrics
            events.append("meta")

    class FakeMemoryRepository:
        """记录记忆过期和写入。"""

        def __init__(self, session: object) -> None:
            del session

        async def expire_fingerprint_bound(
            self,
            source: str,
            fingerprints: set[str],
            *,
            memory_keys: set[str],
        ) -> None:
            del source, fingerprints, memory_keys
            events.append("expire")

        async def upsert_candidates(self, candidates: object) -> None:
            del candidates
            events.append("memory")
            if fail_memory_write is not None:
                raise fail_memory_write

    class FakeDataSyncRepository:
        """记录 durable generation handoff。"""

        def __init__(self, session: object) -> None:
            del session

        async def upsert_desired(self, desired: object) -> None:
            del desired
            events.append("desired")

    class FakeMetadataIndexOutboxRepository:
        """记录与 Meta 同事务发布的索引期望状态。"""

        def __init__(self, session: object) -> None:
            del session

        async def enqueue(self, desired: object) -> None:
            del desired
            events.append("metadata_outbox")

    async def fake_shared_value_refresh_states(
        session: object,
        target_tables: set[str],
    ) -> list[object]:
        """跳过仅与投影内容有关的权威查询。"""
        del session, target_tables
        return []

    monkeypatch.setattr(snapshots, "MetadataRepository", FakeMetadataRepository)
    monkeypatch.setattr(snapshots, "MemoryRepository", FakeMemoryRepository)
    monkeypatch.setattr(snapshots, "DataSyncRepository", FakeDataSyncRepository)
    monkeypatch.setattr(
        snapshots,
        "MetadataIndexOutboxRepository",
        FakeMetadataIndexOutboxRepository,
    )
    monkeypatch.setattr(
        snapshots,
        "shared_value_refresh_states",
        fake_shared_value_refresh_states,
    )


async def test_snapshot_holds_generation_lock_through_session_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发布者必须在事务提交后才释放全部 target generation locks。"""
    events: list[str] = []
    desired = [
        SimpleNamespace(target_table="z_table"),
        SimpleNamespace(target_table="a_table"),
        SimpleNamespace(target_table="a_table"),
    ]

    @asynccontextmanager
    async def generation_locks(
        names: Iterable[str],
        *,
        timeout_seconds: float,
    ) -> AsyncIterator[None]:
        check_equal(
            "publisher 使用目标共享 generation 锁",
            set(names),
            {
                generation_lock_name("dw", "a_table"),
                generation_lock_name("dw", "z_table"),
            },
        )
        events.append(f"generation_enter:{len(set(names))}:{timeout_seconds}")
        yield
        events.append("generation_exit")

    @asynccontextmanager
    async def session() -> AsyncIterator[object]:
        events.append("session_enter")
        yield object()
        events.append("session_commit")

    _install_repository_fakes(monkeypatch, events)
    monkeypatch.setattr(
        snapshots,
        "build_accepted_memories",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        snapshots,
        "build_desired_tables",
        lambda *args, **kwargs: desired,
    )
    monkeypatch.setattr(snapshots.MySQLDatabase, "advisory_locks", generation_locks)
    monkeypatch.setattr(snapshots.MySQLDatabase, "session", session)

    await MySQLAcceptedSnapshotPublisher({"local": "source_demo"}).publish(
        _accepted_snapshot()
    )

    check_equal(
        "发布事务位于 generation lock 内",
        events,
        [
            "generation_enter:2:10",
            "session_enter",
            "fingerprints",
            "expire",
            "meta",
            "desired",
            "metadata_outbox",
            "memory",
            "session_commit",
            "generation_exit",
        ],
    )


async def test_snapshot_rollback_completes_before_generation_lock_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发布事务失败时先回滚，再释放 generation lock 并保留原异常。"""
    events: list[str] = []
    signal = LookupError("memory write failed")

    @asynccontextmanager
    async def generation_locks(
        names: Iterable[str],
        *,
        timeout_seconds: float,
    ) -> AsyncIterator[None]:
        del names, timeout_seconds
        events.append("generation_enter")
        try:
            yield
        finally:
            events.append("generation_exit")

    @asynccontextmanager
    async def session() -> AsyncIterator[object]:
        events.append("session_enter")
        try:
            yield object()
        except BaseException:
            events.append("session_rollback")
            raise

    _install_repository_fakes(monkeypatch, events, fail_memory_write=signal)
    monkeypatch.setattr(
        snapshots,
        "build_accepted_memories",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        snapshots,
        "build_desired_tables",
        lambda *args, **kwargs: [SimpleNamespace(target_table="fact_order")],
    )
    monkeypatch.setattr(snapshots.MySQLDatabase, "advisory_locks", generation_locks)
    monkeypatch.setattr(snapshots.MySQLDatabase, "session", session)

    with pytest.raises(LookupError) as captured:
        await MySQLAcceptedSnapshotPublisher({"local": "source_demo"}).publish(
            _accepted_snapshot()
        )

    check_equal("发布失败保留原异常实例", captured.value is signal, True)
    check_equal(
        "发布回滚早于 generation lock 释放",
        events[-3:],
        ["memory", "session_rollback", "generation_exit"],
    )


async def test_snapshot_release_failure_after_commit_does_not_reverse_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发布提交后的 generation lock 清理故障不得反转快照成功结果。"""
    events: list[str] = []
    warnings: list[str] = []

    @asynccontextmanager
    async def generation_locks(
        names: Iterable[str],
        *,
        timeout_seconds: float,
    ) -> AsyncIterator[None]:
        del names, timeout_seconds
        yield
        raise snapshots.AdvisoryLockReleaseError("owner connection invalidated")

    @asynccontextmanager
    async def session() -> AsyncIterator[object]:
        events.append("session_enter")
        yield object()
        events.append("session_commit")

    _install_repository_fakes(monkeypatch, events)
    monkeypatch.setattr(
        snapshots,
        "build_accepted_memories",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        snapshots,
        "build_desired_tables",
        lambda *args, **kwargs: [SimpleNamespace(target_table="fact_order")],
    )
    monkeypatch.setattr(snapshots.MySQLDatabase, "advisory_locks", generation_locks)
    monkeypatch.setattr(snapshots.MySQLDatabase, "session", session)
    monkeypatch.setattr(snapshots.logger, "warning", warnings.append)

    await MySQLAcceptedSnapshotPublisher({"local": "source_demo"}).publish(
        _accepted_snapshot()
    )

    check_equal("锁清理失败发生前发布事务已提交", events[-1], "session_commit")
    check_equal("提交后锁清理失败只记录一次告警", len(warnings), 1)


async def test_snapshot_lock_contention_is_retryable_and_starts_no_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发布者锁竞争必须返回 503 可重试错误且不进入发布事务。"""
    session_entries: list[str] = []

    @asynccontextmanager
    async def unavailable_lock(
        names: Iterable[str],
        *,
        timeout_seconds: float,
    ) -> AsyncIterator[None]:
        del names, timeout_seconds
        raise snapshots.AdvisoryLockUnavailableError("busy")
        yield

    @asynccontextmanager
    async def session() -> AsyncIterator[object]:
        session_entries.append("entered")
        yield object()

    monkeypatch.setattr(
        snapshots,
        "build_accepted_memories",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        snapshots,
        "build_desired_tables",
        lambda *args, **kwargs: [SimpleNamespace(target_table="fact_order")],
    )
    monkeypatch.setattr(snapshots.MySQLDatabase, "advisory_locks", unavailable_lock)
    monkeypatch.setattr(snapshots.MySQLDatabase, "session", session)

    with pytest.raises(DataAgentError) as captured:
        await MySQLAcceptedSnapshotPublisher({"local": "source_demo"}).publish(
            _accepted_snapshot()
        )

    check_exception("锁竞争投影为业务错误", captured.value, DataAgentError)
    check_equal("锁竞争错误码", captured.value.code, "generation_lock_unavailable")
    check_equal("锁竞争允许上层安全重试", captured.value.retryable, True)
    check_equal("锁竞争 HTTP 状态", captured.value.http_status, 503)
    check_equal("锁竞争前未进入发布事务", session_entries, [])
