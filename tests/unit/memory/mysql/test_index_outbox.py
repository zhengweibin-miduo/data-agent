"""记忆派生索引期望状态仓储的语句级契约测试。"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ClauseElement

from data_agent.memory.mysql.index_outbox import MemoryIndexOutboxRepository
from data_agent.models.memory import (
    MemoryIndexOperation,
    MemoryIndexTarget,
    MemoryOutboxItem,
    MemoryStatus,
)
from tests.helpers.checks import check_condition, check_equal


class _FakeResult:
    """返回预置结果集的替身，兼容标量与行映射两种读取方式。"""

    def __init__(self, values: list[Any]) -> None:
        """绑定预置结果集。"""
        self._values = values

    def scalars(self) -> _FakeResult:
        """沿用同一替身暴露标量视图。"""
        return self

    def mappings(self) -> _FakeResult:
        """沿用同一替身暴露行映射视图。"""
        return self

    def all(self) -> list[Any]:
        """返回预置结果集。"""
        return self._values

    def scalar_one_or_none(self) -> Any | None:
        """返回预置结果的首项，用于单值查询。"""
        return self._values[0] if self._values else None


class _RecordingSession:
    """记录执行语句并对锁定复核返回预置 ACTIVE 子集。"""

    def __init__(self, locked: list[Any]) -> None:
        """绑定查询应返回的预置结果集。"""
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
        lease_token="lease-1",
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


async def test_retry_outbox_only_targets_the_claimed_generation() -> None:
    """失败回写只能命中本次领取的那一代期望状态。"""
    session = _RecordingSession([])
    repository = MemoryIndexOutboxRepository(cast(AsyncSession, session))
    item = MemoryOutboxItem(
        memory_uid="uid-a",
        target=MemoryIndexTarget.ELASTICSEARCH,
        operation=MemoryIndexOperation.UPSERT,
        projection_version="v2",
        attempts=9,
        lease_token="lease-1",
    )

    await repository.retry_outbox(item, "TimeoutError", 3600)

    rendered = _rendered(session.statements[0])
    compact = rendered.replace(" ", "")
    # set_desired_state 覆盖同一行时会把 attempts 重置为 0、available_at 拉回当前
    # 时刻；这两个条件因此能把迟到回写挡在新一代期望之外，避免它把新内容直接推到
    # 死信上限。
    # 仅靠四元组、甚至再加 attempts 与租约时间都无法区分"同一四元组被重新写入并被
    # 另一个 worker 重新领取"的新一代期望，因此用每次领取重新生成的令牌做 CAS。
    check_condition(
        "按领取代次令牌约束回写",
        "lease_token=%s" in compact,
        actual=rendered,
        expected="WHERE 包含 lease_token 等值条件",
    )
    check_condition(
        "绑定本次领取的令牌值",
        "lease-1" in rendered,
        actual=rendered,
        expected="参数包含本次领取令牌",
    )


async def test_enqueue_convergence_preserves_existing_retry_state() -> None:
    """重建收敛请求只纠正操作，不重置已有行的退避进度。"""
    session = _RecordingSession([MemoryStatus.ACTIVE.value])
    repository = MemoryIndexOutboxRepository(cast(AsyncSession, session))

    await repository.enqueue_convergence("uid-a", MemoryIndexTarget.QDRANT)

    rendered = _rendered(session.statements[1])
    check_condition(
        "按权威状态派生 UPSERT",
        "UPSERT" in rendered,
        actual=rendered,
        expected="ACTIVE 权威行派生 UPSERT 操作",
    )
    check_condition(
        "冲突时不重置尝试次数与可用时间",
        "attempts = %s" not in rendered.split("ON DUPLICATE KEY UPDATE")[1],
        actual=rendered,
        expected="ON DUPLICATE 只更新 operation 与 projection_version",
    )


async def test_claim_outbox_writes_fresh_lease_token() -> None:
    """每次领取都写入新的代次令牌，使迟到结算无法命中新一代。"""
    row = {
        "memory_uid": "uid-a",
        "target": MemoryIndexTarget.ELASTICSEARCH.value,
        "operation": MemoryIndexOperation.UPSERT.value,
        "projection_version": "v2",
        "attempts": 0,
    }
    tokens: list[str] = []
    for _ in range(2):
        session = _RecordingSession([row])
        repository = MemoryIndexOutboxRepository(cast(AsyncSession, session))
        items = await repository.claim_outbox(10)
        tokens.append(items[0].lease_token)
        lease = _rendered(session.statements[1])
        check_condition(
            "领取语句写入代次令牌",
            "lease_token" in lease and items[0].lease_token in lease,
            actual=lease,
            expected="UPDATE 同时写入 available_at 与 lease_token",
        )
    check_condition(
        "两次领取的令牌互不相同",
        tokens[0] != tokens[1],
        actual=tokens,
        expected="令牌不可复用",
    )


async def test_late_settlement_cannot_touch_a_newly_claimed_generation() -> None:
    """被重新领取后，旧 worker 的确认与退避都不得命中新一代。"""
    # W1 领取内容 A → 内容 B 以相同四元组覆盖该行 → W2 领取 B（写入新令牌）。
    # 此时 W1 的迟到结算若仍能命中，会把 B 推向死信并缩短 W2 的租约。
    stale = MemoryOutboxItem(
        memory_uid="uid-a",
        target=MemoryIndexTarget.ELASTICSEARCH,
        operation=MemoryIndexOperation.UPSERT,
        projection_version="v2",
        attempts=0,
        lease_token="lease-w1",
    )
    session = _RecordingSession([])
    repository = MemoryIndexOutboxRepository(cast(AsyncSession, session))

    await repository.retry_outbox(stale, "TimeoutError", 3600)
    await repository.acknowledge_outbox(stale, content_hash="hash-1")

    for label, statement in (
        ("退避", session.statements[0]),
        ("确认", session.statements[1]),
    ):
        rendered = _rendered(statement)
        check_condition(
            f"{label}按旧令牌约束",
            "lease-w1" in rendered and "lease_token" in rendered,
            actual=rendered,
            expected="WHERE 绑定发起方自己的领取令牌",
        )
