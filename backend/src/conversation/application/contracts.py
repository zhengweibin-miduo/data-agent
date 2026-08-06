"""Conversation 应用用例依赖的深 interface。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from conversation.models import (
    ClaimedExtraction,
    ConversationPage,
    ConversationRecord,
    ExtractionResult,
    MessagePage,
    MessageRecord,
)
from models.memory import MemoryCandidate, MemoryDetail


@dataclass(frozen=True, slots=True)
class StartedConversationTurn:
    """已提交用户消息及构建上下文所需的会话快照。"""

    message: MessageRecord
    conversation_id: int
    summary: str | None
    summary_through_message_id: int | None
    execution_owner: bool = True


class ConversationStore(Protocol):
    """封装 Conversation 用例所需的短 MySQL 事务。"""

    async def create(self, user_id: str) -> ConversationRecord:
        """创建用户会话。"""
        ...

    async def list(
        self,
        user_id: str,
        *,
        before: int | None,
        limit: int,
    ) -> ConversationPage:
        """分页读取用户会话。"""
        ...

    async def history(
        self,
        user_id: str,
        conversation_uid: str,
        *,
        before: int | None,
        limit: int,
    ) -> MessagePage | None:
        """分页读取会话消息。"""
        ...

    async def delete(self, user_id: str, conversation_uid: str) -> bool:
        """删除单个会话及其 Conversation 权威状态。"""
        ...

    async def start_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        content: str,
    ) -> StartedConversationTurn:
        """原子写入用户消息并占用轮次门禁。"""
        ...

    async def complete_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        content: str,
    ) -> MessageRecord:
        """原子写入助手消息、outbox 并释放轮次门禁。"""
        ...

    async def abandon_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
    ) -> None:
        """释放失败或取消的活动轮次门禁。"""
        ...

    async def assistant_message(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
    ) -> MessageRecord | None:
        """读取幂等回放所需的助手消息。"""
        ...

    async def context_messages(
        self,
        user_id: str,
        conversation_id: int,
        *,
        after_id: int | None,
        limit: int,
    ) -> list[MessageRecord]:
        """读取摘要游标后的有界消息。"""
        ...


class LongTermMemoryReader(Protocol):
    """读取经权威回查的同租户 Long-term Memory。"""

    async def recall(
        self,
        query: str,
        user_id: str,
        *,
        limit: int,
    ) -> Sequence[MemoryDetail]:
        """召回 Conversation 上下文允许使用的用户记忆。"""
        ...


class UserDataEraser(Protocol):
    """原子删除用户 Conversation 并 tombstone Long-term Memory。"""

    async def erase(self, user_id: str) -> None:
        """执行同一事务内的跨上下文用户数据删除。"""
        ...


class ExtractionClaimStore(Protocol):
    """封装提炼任务的短 claim 与 retry 事务。"""

    async def claim(
        self,
        *,
        limit: int,
        lease_seconds: int,
        message_limit: int,
    ) -> list[ClaimedExtraction]:
        """领取一个有界任务波次并提交租约。"""
        ...

    async def retry(self, claim: ClaimedExtraction, error_type: str) -> None:
        """释放当前租约并按数据库时钟登记退避。"""
        ...


class ExtractionCommitter(Protocol):
    """原子提交 validated candidates 与 Conversation 提炼完成态。"""

    async def commit(
        self,
        claim: ClaimedExtraction,
        candidates: list[MemoryCandidate],
        summary: str,
    ) -> None:
        """在单一事务中写 Memory 权威状态并完成提炼任务。"""
        ...


class ExtractionModel(Protocol):
    """在数据库事务外生成结构化提炼结果。"""

    async def extract(self, claim: ClaimedExtraction) -> ExtractionResult:
        """根据已领取的有界消息生成摘要和候选。"""
        ...
