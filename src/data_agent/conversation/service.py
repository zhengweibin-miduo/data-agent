"""Agent 对话用例、事务和有界上下文组装。"""

from data_agent.conversation.models import (
    CompleteTurnResponse,
    ContextMessage,
    ConversationContext,
    ConversationPage,
    ConversationRecord,
    DeleteConversationDataResponse,
    DeleteConversationResponse,
    MessagePage,
    StartTurnResponse,
)
from data_agent.conversation.repository import ConversationRepository
from data_agent.errors import DataAgentError
from data_agent.identifiers import CONVERSATION_MEMORY_SOURCE
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.memory.application.search import MemorySearchService
from data_agent.memory.mysql.repository import MemoryRepository
from data_agent.models.memory import BuiltinMemoryCategory
from data_agent.settings import app_config


class ConversationService:
    """提供永久会话、幂等轮次和同用户上下文。"""

    def __init__(self) -> None:
        """创建共享记忆搜索依赖。"""
        self._memory_search = MemorySearchService()

    async def create(self, user_id: str) -> ConversationRecord:
        """创建用户会话。"""
        async with MySQLDatabase.session() as session:
            return await ConversationRepository(session).create(user_id)

    async def list(
        self,
        user_id: str,
        *,
        before: int | None,
        limit: int,
    ) -> ConversationPage:
        """分页读取用户会话。"""
        async with MySQLDatabase.session() as session:
            return await ConversationRepository(session).list(
                user_id,
                before=before,
                limit=limit,
            )

    async def history(
        self,
        user_id: str,
        conversation_uid: str,
        *,
        before: int | None,
        limit: int,
    ) -> MessagePage:
        """分页读取用户会话的完整原始消息。"""
        async with MySQLDatabase.session() as session:
            page = await ConversationRepository(session).history(
                user_id,
                conversation_uid,
                before=before,
                limit=limit,
            )
        if page is None:
            raise DataAgentError(
                "conversation_not_found",
                "conversation_history",
                "会话不存在",
                http_status=404,
            )
        return page

    async def delete(
        self,
        user_id: str,
        conversation_uid: str,
    ) -> DeleteConversationResponse:
        """删除会话但保留已经跨会话共享的长期记忆。"""
        async with MySQLDatabase.session() as session:
            deleted = await ConversationRepository(session).delete(
                user_id,
                conversation_uid,
            )
        if not deleted:
            raise DataAgentError(
                "conversation_not_found",
                "conversation_delete",
                "会话不存在",
                http_status=404,
            )
        return DeleteConversationResponse(conversation_uid=conversation_uid)

    async def start_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        content: str,
    ) -> StartTurnResponse:
        """先提交用户消息，再读取有界上下文。"""
        async with MySQLDatabase.session() as session:
            message, conversation = await ConversationRepository(session).start_turn(
                user_id, conversation_uid, turn_uid, content
            )
        context = await self._context(
            user_id,
            int(conversation["id"]),
            conversation["summary"],
            conversation["summary_through_message_id"],
            content,
        )
        return StartTurnResponse(message=message, context=context)

    async def complete_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        content: str,
    ) -> CompleteTurnResponse:
        """提交助手消息和提炼 outbox 后才报告完成。"""
        async with MySQLDatabase.session() as session:
            message = await ConversationRepository(session).complete_turn(
                user_id,
                conversation_uid,
                turn_uid,
                content,
            )
        return CompleteTurnResponse(message=message)

    async def delete_user_data(
        self,
        user_id: str,
    ) -> DeleteConversationDataResponse:
        """立即删除会话并 tombstone 该用户长期记忆。"""
        # 先清除会话及其 outbox，再 tombstone 长期记忆；两步共享事务，
        # 避免删除请求留下仍可检索的孤立记忆。
        async with MySQLDatabase.session() as session:
            await ConversationRepository(session).delete_user_conversations(user_id)
            await MemoryRepository(session).tombstone_user(user_id)
        return DeleteConversationDataResponse()

    async def _context(
        self,
        user_id: str,
        conversation_id: int,
        summary_value: object,
        summary_through: object,
        query: str,
    ) -> ConversationContext:
        """按字符预算组装摘要、最近消息和相关用户记忆。"""
        async with MySQLDatabase.session() as session:
            messages = await ConversationRepository(session).context_messages(
                user_id,
                conversation_id,
                after_id=(
                    int(str(summary_through)) if summary_through is not None else None
                ),
                limit=app_config.conversation.context_message_limit,
            )
        response = await self._memory_search.search(
            query,
            CONVERSATION_MEMORY_SOURCE,
            user_id=user_id,
            categories={
                BuiltinMemoryCategory.USER_PROFILE.value,
                BuiltinMemoryCategory.USER_PREFERENCE.value,
                BuiltinMemoryCategory.USER_CONSTRAINT.value,
                BuiltinMemoryCategory.USER_BUSINESS_RULE.value,
            },
            limit=app_config.memory.search_limit,
        )
        # 字符预算从最新消息向前保留，再反转回时间线顺序，确保新上下文优先。
        remaining = app_config.conversation.context_max_chars
        bounded: list[ContextMessage] = []
        for message in reversed(messages):
            if remaining <= 0:
                break
            text = message.content[-remaining:]
            bounded.append(ContextMessage(role=message.role, content=text))
            remaining -= len(text)
        bounded.reverse()
        summary = (
            str(summary_value)[: app_config.conversation.summary_max_chars]
            if summary_value is not None
            else None
        )
        return ConversationContext(
            summary=summary,
            messages=bounded,
            memories=[hit.memory for hit in response.items],
        )
