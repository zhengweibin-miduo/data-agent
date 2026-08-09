"""Conversation 用例与有界上下文组装。"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from conversation.application.contracts import (
    ConversationStore,
    LongTermMemoryReader,
    UserDataEraser,
)
from conversation.models import (
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
from errors import DataAgentError


class ConversationService:
    """提供永久会话、幂等轮次和同用户上下文。"""

    def __init__(
        self,
        store: ConversationStore,
        memories: LongTermMemoryReader,
        user_data: UserDataEraser,
        *,
        context_message_limit: int,
        context_max_chars: int,
        summary_max_chars: int,
        memory_search_limit: int,
    ) -> None:
        """绑定 use-case store、跨上下文 interface 与显式预算。"""
        self._store = store
        self._memories = memories
        self._user_data = user_data
        self._context_message_limit = context_message_limit
        self._context_max_chars = context_max_chars
        self._summary_max_chars = summary_max_chars
        self._memory_search_limit = memory_search_limit

    async def create(self, user_id: str) -> ConversationRecord:
        """创建用户会话。"""
        return await self._store.create(user_id)

    async def list(
        self,
        user_id: str,
        *,
        before: int | None,
        limit: int,
    ) -> ConversationPage:
        """分页读取用户会话。"""
        return await self._store.list(user_id, before=before, limit=limit)

    async def history(
        self,
        user_id: str,
        conversation_uid: str,
        *,
        before: int | None,
        limit: int,
    ) -> MessagePage:
        """分页读取用户会话的完整原始消息。"""
        page = await self._store.history(
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
        deleted = await self._store.delete(user_id, conversation_uid)
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
        *,
        semantic_fingerprint: str | None = None,
        include_context: bool = True,
    ) -> StartTurnResponse:
        """提交用户消息后，基于已提交状态构建同用户的有界上下文。"""
        # 步骤一：store 在短事务中提交消息与活动轮次门禁，再返回会话快照。
        if semantic_fingerprint is None:
            started = await self._store.start_turn(
                user_id, conversation_uid, turn_uid, content
            )
        else:
            started = await self._store.start_turn(
                user_id,
                conversation_uid,
                turn_uid,
                content,
                semantic_fingerprint=semantic_fingerprint,
            )
        if not include_context:
            return StartTurnResponse(
                message=started.message,
                context=ConversationContext(summary=None, messages=[], memories=[]),
                execution_owner=started.execution_owner,
                claim_token=started.claim_token,
                conversation_id=started.conversation_id,
                summary=started.summary,
                summary_through_message_id=started.summary_through_message_id,
            )
        # 已完成轮次的幂等回放不依赖长期记忆或远程检索。
        if not started.execution_owner:
            existing_assistant = await self._store.assistant_message(
                user_id, conversation_uid, turn_uid
            )
            if existing_assistant is not None:
                return StartTurnResponse(
                    message=started.message,
                    context=ConversationContext(summary=None, messages=[], memories=[]),
                    execution_owner=False,
                    claim_token=None,
                )
        # 步骤二：提交完成后再召回消息与长期记忆，模型或索引延迟不持有行锁。
        try:
            context = await self._context(
                user_id,
                started.conversation_id,
                started.summary,
                started.summary_through_message_id,
                content,
            )
        except BaseException:
            if started.execution_owner and started.claim_token is not None:
                try:
                    await self._store.abandon_turn(
                        user_id, conversation_uid, turn_uid, started.claim_token
                    )
                except Exception:
                    # 清理失败不得覆盖上下文、记忆召回或取消的原始异常。
                    pass
            raise
        return StartTurnResponse(
            message=started.message,
            context=context,
            execution_owner=started.execution_owner,
            claim_token=started.claim_token,
            conversation_id=started.conversation_id,
            summary=started.summary,
            summary_through_message_id=started.summary_through_message_id,
        )

    async def start_public_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        content: str,
    ) -> StartTurnResponse:
        """认领公开两步轮次，并在上下文召回期间保持租约。"""
        started = await self.start_turn(
            user_id,
            conversation_uid,
            turn_uid,
            content,
            include_context=False,
        )
        if not started.execution_owner or started.claim_token is None:
            return started
        claim_token = started.claim_token

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(0.25)
                try:
                    renewed = await self.renew_turn(
                        user_id, conversation_uid, turn_uid, claim_token
                    )
                except Exception:
                    # 瞬时传输失败不足以证明 claim 已丢失；下一周期重试。
                    continue
                if not renewed:
                    raise DataAgentError(
                        "conversation_lease_lost",
                        "conversation_turn_renew",
                        "轮次执行权已失效，请使用原 turn_uid 重试",
                        http_status=409,
                        retryable=True,
                    )

        owner_task = asyncio.current_task()
        heartbeat_task = asyncio.create_task(heartbeat())

        def fence_owner(task: asyncio.Task[None]) -> None:
            if owner_task is not None and not task.cancelled() and task.exception():
                owner_task.cancel("conversation_lease_lost")

        heartbeat_task.add_done_callback(fence_owner)
        try:
            context = await self.load_turn_context(user_id, started, content)
        except BaseException:
            try:
                await self.abandon_turn(
                    user_id,
                    conversation_uid,
                    turn_uid,
                    claim_token,
                )
            except Exception:
                # 清理失败不得覆盖上下文、记忆召回或取消的原始异常。
                pass
            raise
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
        return started.model_copy(update={"context": context})

    async def load_turn_context(
        self, user_id: str, started: StartTurnResponse, query: str
    ) -> ConversationContext:
        """在 claim 已返回给调用方后加载上下文，使调用方可先启动续租。"""
        if started.conversation_id is None:
            raise ValueError("轮次响应缺少会话上下文坐标")
        return await self._context(
            user_id,
            started.conversation_id,
            started.summary,
            started.summary_through_message_id,
            query,
        )

    async def complete_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        claim_token: str,
        content: str,
        *,
        semantic_fingerprint: str | None = None,
    ) -> CompleteTurnResponse:
        """提交助手消息和提炼 outbox 后才报告完成。"""
        message = await self._store.complete_turn(
            user_id,
            conversation_uid,
            turn_uid,
            claim_token,
            content,
            semantic_fingerprint=semantic_fingerprint,
        )
        return CompleteTurnResponse(message=message)

    async def abandon_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        claim_token: str,
    ) -> None:
        """释放失败或取消的活动轮次门禁，使同轮次可安全重试。"""
        await self._store.abandon_turn(user_id, conversation_uid, turn_uid, claim_token)

    async def renew_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        claim_token: str,
    ) -> bool:
        """为仍存活的长轮次续租，避免健康 owner 被超时重入。"""
        return await self._store.renew_turn(
            user_id, conversation_uid, turn_uid, claim_token
        )

    async def assistant_message(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
    ) -> MessageRecord | None:
        """读取指定轮次已有的助手消息，供编排入口执行幂等回放。"""
        return await self._store.assistant_message(
            user_id,
            conversation_uid,
            turn_uid,
        )

    async def pending_query_chain(
        self,
        user_id: str,
        conversation_uid: str,
        *,
        through_id: int,
        message_limit: int,
        max_chars: int,
    ) -> list[MessageRecord]:
        """从独立权威预算读取当前 Query 澄清证据链。"""
        return await self._store.pending_query_chain(
            user_id,
            conversation_uid,
            through_id=through_id,
            message_limit=message_limit,
            max_chars=max_chars,
        )

    async def delete_user_data(
        self,
        user_id: str,
    ) -> DeleteConversationDataResponse:
        """立即删除会话并 tombstone 该用户长期记忆。"""
        await self._user_data.erase(user_id)
        return DeleteConversationDataResponse()

    async def _context(
        self,
        user_id: str,
        conversation_id: int,
        summary: str | None,
        summary_through: int | None,
        query: str,
    ) -> ConversationContext:
        """组装摘要游标后的消息与同用户长期记忆，并分别施加预算。"""
        # 步骤一：Conversation store 读取摘要游标后的同租户最新消息窗口。
        messages = await self._store.context_messages(
            user_id,
            conversation_id,
            after_id=summary_through,
            limit=self._context_message_limit,
        )
        # 步骤二：reader 隐藏 Memory source/category 与权威回查实现。
        memories = await self._memories.recall(
            query,
            user_id,
            limit=self._memory_search_limit,
        )
        # 步骤三：从最新消息向前应用字符预算，再恢复时间线顺序。
        remaining = self._context_max_chars
        bounded: list[ContextMessage] = []
        for message in reversed(messages):
            if remaining <= 0:
                break
            text = message.content[-remaining:]
            bounded.append(
                ContextMessage(
                    role=message.role,
                    content=text,
                    semantic_fingerprint=message.semantic_fingerprint,
                )
            )
            remaining -= len(text)
        bounded.reverse()
        # 步骤四：摘要、近期消息和 Long-term Memory 保持独立预算。
        return ConversationContext(
            summary=(
                summary[: self._summary_max_chars] if summary is not None else None
            ),
            messages=bounded,
            memories=list(memories),
        )
