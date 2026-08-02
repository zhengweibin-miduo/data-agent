"""Conversation 用例级 MySQL store。"""

import builtins

from data_agent.conversation.application.contracts import StartedConversationTurn
from data_agent.conversation.models import (
    ConversationPage,
    ConversationRecord,
    MessagePage,
    MessageRecord,
)
from data_agent.conversation.repository import ConversationRepository
from data_agent.infrastructure.mysql import MySQLDatabase


class MySQLConversationStore:
    """用独立托管事务实现 ConversationStore。"""

    async def create(self, user_id: str) -> ConversationRecord:
        """创建用户会话。"""
        async with MySQLDatabase.session() as session:
            return await ConversationRepository(session).create(user_id)

    async def list(
        self, user_id: str, *, before: int | None, limit: int
    ) -> ConversationPage:
        """分页读取用户会话。"""
        async with MySQLDatabase.session() as session:
            return await ConversationRepository(session).list(
                user_id, before=before, limit=limit
            )

    async def history(
        self,
        user_id: str,
        conversation_uid: str,
        *,
        before: int | None,
        limit: int,
    ) -> MessagePage | None:
        """分页读取会话消息。"""
        async with MySQLDatabase.session() as session:
            return await ConversationRepository(session).history(
                user_id, conversation_uid, before=before, limit=limit
            )

    async def delete(self, user_id: str, conversation_uid: str) -> bool:
        """删除单个用户会话。"""
        async with MySQLDatabase.session() as session:
            return await ConversationRepository(session).delete(
                user_id, conversation_uid
            )

    async def start_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        content: str,
    ) -> StartedConversationTurn:
        """原子写入用户消息并占用轮次门禁。"""
        async with MySQLDatabase.session() as session:
            message, conversation = await ConversationRepository(session).start_turn(
                user_id, conversation_uid, turn_uid, content
            )
        return StartedConversationTurn(
            message=message,
            conversation_id=int(conversation["id"]),
            summary=(
                str(conversation["summary"])
                if conversation["summary"] is not None
                else None
            ),
            summary_through_message_id=(
                int(conversation["summary_through_message_id"])
                if conversation["summary_through_message_id"] is not None
                else None
            ),
        )

    async def complete_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        content: str,
    ) -> MessageRecord:
        """原子写入助手消息、outbox 并释放轮次门禁。"""
        async with MySQLDatabase.session() as session:
            return await ConversationRepository(session).complete_turn(
                user_id, conversation_uid, turn_uid, content
            )

    async def assistant_message(
        self, user_id: str, conversation_uid: str, turn_uid: str
    ) -> MessageRecord | None:
        """读取幂等回放所需的助手消息。"""
        async with MySQLDatabase.session() as session:
            return await ConversationRepository(session).assistant_message(
                user_id, conversation_uid, turn_uid
            )

    async def context_messages(
        self,
        user_id: str,
        conversation_id: int,
        *,
        after_id: int | None,
        limit: int,
    ) -> builtins.list[MessageRecord]:
        """读取摘要游标后的有界消息。"""
        async with MySQLDatabase.session() as session:
            return await ConversationRepository(session).context_messages(
                user_id, conversation_id, after_id=after_id, limit=limit
            )
