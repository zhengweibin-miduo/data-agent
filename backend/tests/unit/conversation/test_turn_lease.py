"""活动对话轮次门禁租约的语句级与分支契约测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ClauseElement

from conversation.models import MessageRole
from conversation.repository import ConversationRepository
from errors import DataAgentError
from settings import app_config
from tests.helpers.checks import check_condition, check_equal


class _FakeResult:
    """返回预置单行结果的替身。"""

    def __init__(self, row: object | None) -> None:
        """绑定预置行。"""
        self._row = row

    def mappings(self) -> _FakeResult:
        """沿用同一替身暴露行映射视图。"""
        return self

    def one_or_none(self) -> object | None:
        """返回预置行或 None。"""
        return self._row

    def first(self) -> object | None:
        """返回预置行或 None，用于存在性查询。"""
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
        and "coalesce(" in rendered
        and "turn_abandoned_at" in rendered,
        actual=rendered,
        expected="判定以 OR 覆盖空闲、同轮次与超租约三种情形",
    )
    check_condition(
        "放弃轮次使用独立有限租约",
        "coalesce(" in rendered.casefold() and "turn_abandoned_at" in rendered,
        actual=rendered,
        expected="失败时间参与有限租约判定",
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


class _ReplaySession(_RecordingSession):
    """按顺序返回会话行、既有用户消息，并记录门禁续租语句。"""

    def __init__(
        self,
        conversation: dict[str, object],
        message: dict[str, object],
        assistant: object | None = None,
    ):
        """绑定预置的会话行与既有用户消息。"""
        super().__init__(claimable=True)
        self._rows = [conversation, message, assistant]
        self._index = 0

    async def execute(self, statement: ClauseElement) -> object:
        """按调用顺序返回会话、门禁判定、既有消息与助手消息存在性。"""
        self.statements.append(statement)
        rendered = str(statement).casefold()
        if rendered.startswith("update"):
            return _FakeResult(None)
        if "timestampadd" in rendered:
            # 初始门禁查询包含 OR；独立的自然过期判定默认模拟仍在租约内。
            return _FakeResult((1,) if " or " in rendered else None)
        row = self._rows[min(self._index, len(self._rows) - 1)]
        self._index += 1
        return _FakeResult(row)


async def test_idempotent_replay_does_not_renew_turn_lease() -> None:
    """非所有者轮询不得延长崩溃进程遗留的租约。"""
    moment = datetime.now(UTC)
    conversation = {"id": 1, "active_turn_uid": "turn-1", "updated_at": moment}
    message = {
        "id": 7,
        "uid": "message-7",
        "turn_uid": "turn-1",
        "role": MessageRole.USER.value,
        "content": "订单口径是什么",
        "created_at": moment,
    }
    session = _ReplaySession(conversation, message, {"id": 8})
    repository = ConversationRepository(cast(AsyncSession, session))

    record, _, execution_owner = await repository.start_turn(
        "user-1", "conv-1", "turn-1", "订单口径是什么"
    )

    check_equal("幂等回放返回既有消息", record.uid, "message-7")
    check_equal("在途幂等回放不取得执行权", execution_owner, False)
    updates = [s for s in session.statements if str(s).casefold().startswith("update")]
    check_equal("非所有者幂等回放不续租", len(updates), 0)


async def test_query_replay_rejects_changed_semantic_fingerprint() -> None:
    """相同问题但不同 DDL 语义指纹不得回放旧查询结果。"""
    moment = datetime.now(UTC)
    conversation = {"id": 1, "active_turn_uid": None, "updated_at": moment}
    message = {
        "id": 7,
        "uid": "message-7",
        "turn_uid": "turn-1",
        "role": MessageRole.USER.value,
        "content": "查询销售额",
        "semantic_fingerprint": "a" * 64,
        "created_at": moment,
    }
    repository = ConversationRepository(
        cast(AsyncSession, _ReplaySession(conversation, message))
    )

    with pytest.raises(DataAgentError) as caught:
        await repository.start_turn(
            "user-1",
            "conv-1",
            "turn-1",
            "查询销售额",
            semantic_fingerprint="b" * 64,
        )

    assert caught.value.code == "idempotency_conflict"


async def test_completed_turn_replay_does_not_revive_gate() -> None:
    """已完成轮次的 start_turn 重放不得让门禁复活。"""
    # complete_turn 已清空门禁，且它的幂等返回路径不会再次清理；重新占用会在整个
    # 租约期内把该会话的新轮次挡在 409 之外。
    moment = datetime.now(UTC)
    conversation = {"id": 1, "active_turn_uid": None, "updated_at": moment}
    message = {
        "id": 7,
        "uid": "message-7",
        "turn_uid": "turn-1",
        "role": MessageRole.USER.value,
        "content": "订单口径是什么",
        "created_at": moment,
    }
    session = _ReplaySession(conversation, message, {"id": 8})
    repository = ConversationRepository(cast(AsyncSession, session))

    record, _, execution_owner = await repository.start_turn(
        "user-1",
        "conv-1",
        "turn-1",
        "订单口径是什么",
    )

    check_equal("仍返回既有用户消息", record.uid, "message-7")
    check_equal("已完成轮次不取得执行权", execution_owner, False)
    updates = [s for s in session.statements if str(s).casefold().startswith("update")]
    check_equal("已完成轮次不重新占用门禁", updates, [])


async def test_abandoned_turn_replay_reclaims_execution_owner() -> None:
    """失败释放后的同一 turn_uid 能原子续租并重新取得执行权。"""
    conversation = {
        "id": 1,
        "active_turn_uid": "turn-1",
        "updated_at": datetime(1970, 1, 1, tzinfo=UTC),
    }
    message = {
        "id": 7,
        "uid": "message-7",
        "turn_uid": "turn-1",
        "role": MessageRole.USER.value,
        "content": "订单口径是什么",
        "created_at": datetime.now(UTC),
    }
    repository = ConversationRepository(
        cast(AsyncSession, _ReplaySession(conversation, message))
    )

    _, _, execution_owner = await repository.start_turn(
        "user-1", "conv-1", "turn-1", "订单口径是什么"
    )

    check_equal("失败轮次重新取得执行权", execution_owner, True)


async def test_preempted_turn_replay_does_not_reclaim_gate() -> None:
    """被后续轮次抢占的陈旧轮次，延迟重试时不得重新占用门禁。"""
    # A 超租约 → B 抢占并完成（门禁已清空）→ A 延迟重试。若在此重新占用门禁，
    # complete_turn(A) 会被接受，形成 user(A) user(B) assistant(B) assistant(A)
    # 的错乱顺序，并污染后续摘要与记忆提炼。
    moment = datetime.now(UTC)
    conversation = {"id": 1, "active_turn_uid": None, "updated_at": moment}
    message = {
        "id": 7,
        "uid": "message-7",
        "turn_uid": "turn-a",
        "role": MessageRole.USER.value,
        "content": "订单口径是什么",
        "created_at": moment,
    }
    session = _ReplaySession(conversation, message)
    repository = ConversationRepository(cast(AsyncSession, session))

    await repository.start_turn("user-1", "conv-1", "turn-a", "订单口径是什么")

    updates = [s for s in session.statements if str(s).casefold().startswith("update")]
    check_equal("陈旧轮次不重新占用门禁", updates, [])


class _ExpiredReplaySession(_ReplaySession):
    """让独立租约查询返回已过期。"""

    async def execute(self, statement: ClauseElement) -> object:
        rendered = str(statement).casefold()
        if "timestampadd" in rendered and " or " not in rendered:
            self.statements.append(statement)
            return _FakeResult((1,))
        return await super().execute(statement)


async def test_expired_same_turn_reclaims_execution_owner() -> None:
    """进程崩溃遗留的自然过期同轮次租约可由重试原子重新认领。"""
    moment = datetime.now(UTC)
    conversation = {"id": 1, "active_turn_uid": "turn-1", "updated_at": moment}
    message = {
        "id": 7,
        "uid": "message-7",
        "turn_uid": "turn-1",
        "role": MessageRole.USER.value,
        "content": "订单口径是什么",
        "created_at": moment,
    }
    repository = ConversationRepository(
        cast(AsyncSession, _ExpiredReplaySession(conversation, message))
    )

    _, _, execution_owner = await repository.start_turn(
        "user-1", "conv-1", "turn-1", "订单口径是什么"
    )

    assert execution_owner is True
