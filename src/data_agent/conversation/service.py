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
    MessageRecord,
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
        # 步骤一：在独立托管事务中创建并回读会话，正常退出后统一提交。
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
        # 步骤一：在托管会话中执行带用户边界的稳定游标读取。
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
        # 步骤一：在数据库查询中同时应用用户归属、会话标识和消息游标。
        async with MySQLDatabase.session() as session:
            page = await ConversationRepository(session).history(
                user_id,
                conversation_uid,
                before=before,
                limit=limit,
            )

        # 步骤二：归属不匹配与真实缺失统一映射为安全的会话不存在响应。
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
        # 步骤一：只在用户归属范围内删除会话；共享长期记忆不属于该级联范围。
        async with MySQLDatabase.session() as session:
            deleted = await ConversationRepository(session).delete(
                user_id,
                conversation_uid,
            )

        # 步骤二：没有受影响会话时返回统一的安全未找到错误。
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
        """提交用户消息后，基于已提交状态构建同用户的有界上下文。

        消息写入和活动轮次占用在一个事务中完成；上下文检索在提交后另行
        执行，因此即使摘要或长期记忆召回延迟，已经持久化的消息仍可读取。
        """
        # 步骤一：先在事务中原子写入用户消息并占用活动轮次，提交后消息即可独立读取。
        async with MySQLDatabase.session() as session:
            message, conversation = await ConversationRepository(session).start_turn(
                user_id, conversation_uid, turn_uid, content
            )

        # 步骤二：基于已提交会话快照在事务外组装摘要、近期消息和同用户长期记忆。
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
        # 步骤一：在同一事务内提交助手消息、提炼任务并释放活动轮次门禁。
        async with MySQLDatabase.session() as session:
            message = await ConversationRepository(session).complete_turn(
                user_id,
                conversation_uid,
                turn_uid,
                content,
            )
        return CompleteTurnResponse(message=message)

    async def assistant_message(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
    ) -> MessageRecord | None:
        """读取指定轮次已有的助手消息，供编排入口执行幂等回放。"""
        # 步骤一：在用户和会话边界内执行精确轮次读取，不扫描消息历史页。
        async with MySQLDatabase.session() as session:
            return await ConversationRepository(session).assistant_message(
                user_id,
                conversation_uid,
                turn_uid,
            )

    async def delete_user_data(
        self,
        user_id: str,
    ) -> DeleteConversationDataResponse:
        """立即删除会话并 tombstone 该用户长期记忆。"""
        async with MySQLDatabase.session() as session:
            # 步骤一：先按用户清除会话、消息及其提炼 outbox，停止产生新的记忆。
            await ConversationRepository(session).delete_user_conversations(user_id)

            # 步骤二：在同一事务内 tombstone 该用户长期记忆，避免删除后仍可召回。
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
        """组装摘要游标后的消息与同用户长期记忆，并分别施加预算。

        摘要使用独立字符上限，近期消息同时受数量和总字符预算约束，长期
        记忆受搜索数量上限约束；记忆搜索始终携带 ``user_id``。
        """
        # 步骤一：以摘要游标为下界读取同一用户、同一会话的最新消息窗口；
        # 已折叠进摘要的历史不再重复进入原始消息上下文。
        async with MySQLDatabase.session() as session:
            messages = await ConversationRepository(session).context_messages(
                user_id,
                conversation_id,
                after_id=(
                    int(str(summary_through)) if summary_through is not None else None
                ),
                limit=app_config.conversation.context_message_limit,
            )

        # 步骤二：长期记忆与原始消息使用相同的用户边界；索引只贡献候选，
        # 最终内容仍由记忆服务按权威数据校验后返回。
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

        # 步骤三：字符预算从最新消息向前保留，再反转回时间线顺序；数量窗口
        # 和字符预算共同保证新上下文优先且请求体有界。
        remaining = app_config.conversation.context_max_chars
        bounded: list[ContextMessage] = []
        for message in reversed(messages):
            if remaining <= 0:
                break
            text = message.content[-remaining:]
            bounded.append(ContextMessage(role=message.role, content=text))
            remaining -= len(text)
        bounded.reverse()

        # 步骤四：摘要单独截断后与近期消息、同用户权威记忆汇合，三类来源
        # 保持各自预算，避免任一来源挤占其他上下文。
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
