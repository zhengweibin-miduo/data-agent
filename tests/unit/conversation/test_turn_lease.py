"""活动对话轮次门禁租约的语句级与分支契约测试。"""

from __future__ import annotations

from typing import cast

from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ClauseElement

from data_agent.conversation.repository import ConversationRepository
from data_agent.settings import app_config
from tests.helpers.checks import check_condition, check_equal


class _FakeResult:
    """返回预置单行结果的替身。"""

    def __init__(self, row: object | None) -> None:
        """绑定预置行。"""
        self._row = row

    def one_or_none(self) -> object | None:
        """返回预置行或 None。"""
        return self._row


class _RecordingSession:
    """记录执行语句并按预置结果响应门禁判定。"""

    def __init__(self, *, claimable: bool) -> None:
        """绑定门禁判定应返回的结果。"""
        self.statements: list[ClauseElement] = []
        self._row = (1,) if claimable else None

    async def execute(self, statement: ClauseElement) -> _FakeResult:
        """记录语句并返回预置结果。"""
        self.statements.append(statement)
        return _FakeResult(self._row)


def _rendered(statement: ClauseElement) -> str:
    """渲染语句文本与参数，供断言检查关键子句和取值。"""
    compiled = statement.compile(dialect=mysql.dialect())
    return f"{compiled} {compiled.params}"


async def test_turn_gate_uses_database_side_lease_deadline() -> None:
    """租约到期时间必须由数据库端时间函数计算。"""
    session = _RecordingSession(claimable=True)
    repository = ConversationRepository(cast(AsyncSession, session))

    claimable = await repository._turn_gate_claimable(1, "user-1", "turn-1")

    check_equal("门禁空闲时可占用", claimable, True)
    rendered = _rendered(session.statements[0])
    check_condition(
        "租约判定使用数据库端时间函数",
        "timestampadd" in rendered.casefold() and "now()" in rendered.casefold(),
        actual=rendered,
        expected="判定包含 timestampadd(SECOND, -N, now())",
    )
    check_condition(
        "租约秒数来自配置",
        str(app_config.conversation.turn_lease_seconds) in rendered,
        actual=rendered,
        expected=f"包含 {app_config.conversation.turn_lease_seconds}",
    )
    check_condition(
        "同时覆盖空闲、同轮次重入与超租约三种可占用情形",
        "active_turn_uid IS NULL" in rendered
        and "active_turn_uid = %s" in rendered
        and "updated_at <= timestampadd" in rendered,
        actual=rendered,
        expected="判定以 OR 覆盖空闲、同轮次与超租约三种情形",
    )


async def test_turn_gate_rejects_live_turn_and_allows_expired_turn() -> None:
    """未超租约的在途轮次不可占用，超租约的可被抢占。"""
    live = _RecordingSession(claimable=False)
    check_equal(
        "未超租约的在途轮次不可占用",
        await ConversationRepository(cast(AsyncSession, live))._turn_gate_claimable(
            1,
            "user-1",
            "turn-2",
        ),
        False,
    )
    expired = _RecordingSession(claimable=True)
    check_equal(
        "超过租约的在途轮次可被抢占",
        await ConversationRepository(cast(AsyncSession, expired))._turn_gate_claimable(
            1,
            "user-1",
            "turn-2",
        ),
        True,
    )
