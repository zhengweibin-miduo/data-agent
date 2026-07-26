"""记忆派生索引期望状态仓储的语句级契约测试。"""

from __future__ import annotations

from typing import cast

from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ClauseElement

from data_agent.memory.mysql.index_outbox import MemoryIndexOutboxRepository
from data_agent.models.memory import (
    MemoryIndexOperation,
    MemoryIndexTarget,
    MemoryOutboxItem,
)
from tests.helpers.checks import check_condition, check_equal


class _FakeResult:
    """返回预置标量集合的结果替身。"""

    def __init__(self, values: list[str]) -> None:
        """绑定预置标量。"""
        self._values = values

    def scalars(self) -> _FakeResult:
        """沿用同一替身暴露标量视图。"""
        return self

    def mappings(self) -> _FakeResult:
        """沿用同一替身暴露行映射视图。"""
        return self

    def all(self) -> list[str]:
        """返回预置标量集合。"""
        return self._values


class _RecordingSession:
    """记录执行语句并对锁定复核返回预置 ACTIVE 子集。"""

    def __init__(self, locked: list[str]) -> None:
        """绑定锁定复核应返回的 UID 子集。"""
        self.statements: list[ClauseElement] = []
        self._locked = locked

    async def execute(self, statement: ClauseElement) -> _FakeResult:
        """记录语句并返回预置标量结果。"""
        self.statements.append(statement)
        return _FakeResult(list(self._locked))


def _rendered(statement: ClauseElement) -> str:
    """渲染语句文本与参数，供断言检查关键子句和取值。"""
    compiled = statement.compile(dialect=mysql.dialect())
    return f"{compiled} {compiled.params}"


async def test_enqueue_rebuild_locks_and_filters_active_rows() -> None:
    """重建只能为事务内锁定且仍为 ACTIVE 的行生成期望状态。"""
    session = _RecordingSession(["uid-active"])
    repository = MemoryIndexOutboxRepository(cast(AsyncSession, session))

    await repository.enqueue_rebuild({"uid-active", "uid-deleted"})

    check_equal("语句数量", len(session.statements), 3)
    check_condition(
        "锁定复核使用行锁",
        "FOR UPDATE" in _rendered(session.statements[0]),
        actual=_rendered(session.statements[0]),
        expected="复核 SELECT 携带 FOR UPDATE",
    )
    check_condition(
        "锁定复核只接受 ACTIVE 行",
        "active" in _rendered(session.statements[0]),
        actual=_rendered(session.statements[0]),
        expected="复核 SELECT 过滤 status = active",
    )
    desired_state = _rendered(session.statements[2])
    check_condition(
        "只为锁定的 ACTIVE 行写期望状态",
        "uid-active" in desired_state,
        actual=desired_state,
        expected="期望状态包含锁定的 ACTIVE UID",
    )
    check_condition(
        "并发删除的行不被覆盖为 UPSERT",
        "uid-deleted" not in desired_state,
        actual=desired_state,
        expected="期望状态不包含未锁定的 UID",
    )


async def test_enqueue_rebuild_skips_when_no_row_stays_active() -> None:
    """锁定复核为空时不得写入任何投影版本或期望状态。"""
    session = _RecordingSession([])
    repository = MemoryIndexOutboxRepository(cast(AsyncSession, session))

    await repository.enqueue_rebuild({"uid-deleted"})

    check_equal("仅执行锁定复核语句", len(session.statements), 1)


async def test_acknowledge_outbox_requires_authoritative_consistency() -> None:
    """确认必须同时校验期望条件与权威内容一致性。"""
    session = _RecordingSession([])
    repository = MemoryIndexOutboxRepository(cast(AsyncSession, session))
    item = MemoryOutboxItem(
        memory_uid="uid-a",
        target=MemoryIndexTarget.ELASTICSEARCH,
        operation=MemoryIndexOperation.UPSERT,
        projection_version="v2",
        attempts=0,
    )

    await repository.acknowledge_outbox(item, content_hash="hash-1")
    written = _rendered(session.statements[0])
    check_condition(
        "写入路径要求权威行仍为同一内容",
        "EXISTS" in written and "hash-1" in written,
        actual=written,
        expected="确认条件包含内容一致性 EXISTS 子查询",
    )
    check_condition(
        "写入路径要求权威行仍为 ACTIVE",
        "ACTIVE" in written,
        actual=written,
        expected="确认条件包含 status = ACTIVE",
    )

    await repository.acknowledge_outbox(item, content_hash=None)
    removed = _rendered(session.statements[1])
    check_condition(
        "删除路径要求权威行不再是可检索的 ACTIVE 行",
        "NOT (EXISTS" in removed,
        actual=removed,
        expected="确认条件包含 NOT EXISTS 子查询",
    )


async def test_claim_outbox_filters_dead_letters_and_writes_lease() -> None:
    """领取必须排除死信行并为已领取行写入租约。"""
    session = _RecordingSession([])
    repository = MemoryIndexOutboxRepository(cast(AsyncSession, session))

    await repository.claim_outbox(10)

    claim = _rendered(session.statements[0])
    check_condition(
        "领取使用跳锁行锁",
        "FOR UPDATE SKIP LOCKED" in claim,
        actual=claim,
        expected="领取 SELECT 携带 FOR UPDATE SKIP LOCKED",
    )
    check_condition(
        "领取排除已达尝试上限的行",
        "attempts <" in claim,
        actual=claim,
        expected="领取条件包含 attempts 上限",
    )
    check_equal("空批次不写入租约", len(session.statements), 1)
