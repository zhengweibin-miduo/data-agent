"""带用户隔离和调用方事务边界的对话仓储。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.engine import CursorResult, RowMapping
from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.conversation.models import (
    ClaimedExtraction,
    ConversationPage,
    ConversationRecord,
    MessagePage,
    MessageRecord,
    MessageRole,
)
from data_agent.conversation.mysql_tables import (
    agent_conversation,
    agent_message,
    conversation_memory_outbox,
)
from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.identifiers import stable_id


def _message(row: RowMapping) -> MessageRecord:
    """把数据库行转换为纯文本消息。"""
    return MessageRecord(
        id=int(row["id"]),
        uid=str(row["uid"]),
        turn_uid=str(row["turn_uid"]),
        role=MessageRole(str(row["role"])),
        content=str(row["content"]),
        created_at=row["created_at"],
    )


def _inserted_id(result: object, label: str) -> int:
    """读取 MySQL INSERT 自增主键。"""
    cursor = result if isinstance(result, CursorResult) else None
    key = cursor.inserted_primary_key if cursor is not None else None
    if key is None or key[0] is None:
        raise RuntimeError(f"{label}未返回主键")
    return int(key[0])


class ConversationRepository:
    """管理永久会话、消息和提炼 outbox。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定调用方管理的 Session。"""
        self._session = session

    async def create(self, user_id: str) -> ConversationRecord:
        """创建一个用户会话。"""
        uid = stable_id("conversation", user_id, uuid4().hex)
        result = await self._session.execute(
            insert(agent_conversation).values(uid=uid, user_id=user_id)
        )
        identifier = _inserted_id(result, "会话")
        row = (
            await self._session.execute(
                select(agent_conversation).where(
                    agent_conversation.c.id == identifier,
                    agent_conversation.c.user_id == user_id,
                )
            )
        ).mappings().one()
        return ConversationRecord(
            uid=uid,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    async def list(
        self,
        user_id: str,
        *,
        before: int | None,
        limit: int,
    ) -> ConversationPage:
        """按更新时间与主键稳定读取用户会话。"""
        filters = [agent_conversation.c.user_id == user_id]
        if before is not None:
            filters.append(agent_conversation.c.id < before)
        rows = list(
            (
                await self._session.execute(
                    select(agent_conversation)
                    .where(*filters)
                    .order_by(agent_conversation.c.id.desc())
                    .limit(limit + 1)
                )
            ).mappings()
        )
        visible = rows[:limit]
        return ConversationPage(
            items=[
                ConversationRecord(
                    uid=str(row["uid"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in visible
            ],
            next_before=(
                int(visible[-1]["id"]) if len(rows) > limit and visible else None
            ),
        )

    async def get(
        self,
        user_id: str,
        conversation_uid: str,
        *,
        for_update: bool = False,
    ) -> RowMapping | None:
        """在 SQL 中同时按用户和会话标识读取。"""
        statement = select(agent_conversation).where(
            agent_conversation.c.user_id == user_id,
            agent_conversation.c.uid == conversation_uid,
        )
        if for_update:
            statement = statement.with_for_update()
        return (
            (await self._session.execute(statement)).mappings().one_or_none()
        )

    async def delete(self, user_id: str, conversation_uid: str) -> bool:
        """只删除指定用户拥有的会话。"""
        result = await self._session.execute(
            delete(agent_conversation).where(
                agent_conversation.c.user_id == user_id,
                agent_conversation.c.uid == conversation_uid,
            )
        )
        return bool(getattr(result, "rowcount", 0))

    async def delete_user_conversations(self, user_id: str) -> None:
        """硬删除用户的全部对话数据。"""
        await self._session.execute(
            delete(agent_conversation).where(
                agent_conversation.c.user_id == user_id
            )
        )

    async def history(
        self,
        user_id: str,
        conversation_uid: str,
        *,
        before: int | None,
        limit: int,
    ) -> MessagePage | None:
        """用消息自增主键执行稳定 keyset 分页。"""
        conversation = await self.get(user_id, conversation_uid)
        if conversation is None:
            return None
        filters = [
            agent_message.c.user_id == user_id,
            agent_message.c.conversation_id == int(conversation["id"]),
        ]
        if before is not None:
            filters.append(agent_message.c.id < before)
        rows = list(
            (
                await self._session.execute(
                    select(agent_message)
                    .where(*filters)
                    .order_by(agent_message.c.id.desc())
                    .limit(limit + 1)
                )
            ).mappings()
        )
        visible = rows[:limit]
        return MessagePage(
            items=[_message(row) for row in reversed(visible)],
            next_before=(
                int(visible[-1]["id"]) if len(rows) > limit and visible else None
            ),
        )

    async def start_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        content: str,
    ) -> tuple[MessageRecord, RowMapping]:
        """门禁并幂等持久化用户消息。"""
        conversation = await self.get(
            user_id,
            conversation_uid,
            for_update=True,
        )
        if conversation is None:
            raise DDLMetadataError(
                "conversation_not_found",
                "conversation_turn",
                "会话不存在",
                http_status=404,
            )
        active_turn = conversation["active_turn_uid"]
        if active_turn is not None and str(active_turn) != turn_uid:
            raise DDLMetadataError(
                "conversation_busy",
                "conversation_turn",
                "会话已有在途轮次",
                http_status=409,
            )
        existing = (
            await self._session.execute(
                select(agent_message).where(
                    agent_message.c.user_id == user_id,
                    agent_message.c.conversation_id == int(conversation["id"]),
                    agent_message.c.turn_uid == turn_uid,
                    agent_message.c.role == MessageRole.USER.value,
                )
            )
        ).mappings().one_or_none()
        if existing is not None:
            if str(existing["content"]) != content:
                raise DDLMetadataError(
                    "idempotency_conflict",
                    "conversation_turn",
                    "相同轮次的用户内容不一致",
                    http_status=409,
                )
            return _message(existing), conversation
        uid = stable_id("message", conversation_uid, turn_uid, MessageRole.USER.value)
        result = await self._session.execute(
            insert(agent_message).values(
                uid=uid,
                user_id=user_id,
                conversation_id=int(conversation["id"]),
                turn_uid=turn_uid,
                role=MessageRole.USER.value,
                content=content,
            )
        )
        message_id = _inserted_id(result, "用户消息")
        await self._session.execute(
            update(agent_conversation)
            .where(
                agent_conversation.c.id == int(conversation["id"]),
                agent_conversation.c.user_id == user_id,
            )
            .values(active_turn_uid=turn_uid, updated_at=func.now())
        )
        row = (
            await self._session.execute(
                select(agent_message).where(agent_message.c.id == message_id)
            )
        ).mappings().one()
        return _message(row), conversation

    async def complete_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        content: str,
    ) -> MessageRecord:
        """原子持久化助手消息、提炼任务并清除门禁。"""
        conversation = await self.get(
            user_id,
            conversation_uid,
            for_update=True,
        )
        if conversation is None:
            raise DDLMetadataError(
                "conversation_not_found",
                "conversation_complete",
                "会话不存在",
                http_status=404,
            )
        conversation_id = int(conversation["id"])
        messages = list(
            (
                await self._session.execute(
                    select(agent_message).where(
                        agent_message.c.user_id == user_id,
                        agent_message.c.conversation_id == conversation_id,
                        agent_message.c.turn_uid == turn_uid,
                    )
                )
            ).mappings()
        )
        by_role = {str(row["role"]): row for row in messages}
        user_message = by_role.get(MessageRole.USER.value)
        if user_message is None:
            raise DDLMetadataError(
                "turn_not_started",
                "conversation_complete",
                "轮次尚未持久化用户消息",
                http_status=409,
            )
        existing = by_role.get(MessageRole.ASSISTANT.value)
        if existing is not None:
            if str(existing["content"]) != content:
                raise DDLMetadataError(
                    "idempotency_conflict",
                    "conversation_complete",
                    "相同轮次的助手内容不一致",
                    http_status=409,
                )
            return _message(existing)
        if str(conversation["active_turn_uid"] or "") != turn_uid:
            raise DDLMetadataError(
                "stale_turn",
                "conversation_complete",
                "轮次不是当前在途轮次",
                http_status=409,
            )
        uid = stable_id(
            "message",
            conversation_uid,
            turn_uid,
            MessageRole.ASSISTANT.value,
        )
        result = await self._session.execute(
            insert(agent_message).values(
                uid=uid,
                user_id=user_id,
                conversation_id=conversation_id,
                turn_uid=turn_uid,
                role=MessageRole.ASSISTANT.value,
                content=content,
            )
        )
        assistant_id = _inserted_id(result, "助手消息")
        outbox = insert(conversation_memory_outbox).values(
            user_id=user_id,
            conversation_id=conversation_id,
            turn_uid=turn_uid,
            user_message_id=int(user_message["id"]),
            assistant_message_id=assistant_id,
        )
        await self._session.execute(
            outbox.on_duplicate_key_update(
                assistant_message_id=outbox.inserted.assistant_message_id
            )
        )
        await self._session.execute(
            update(agent_conversation)
            .where(
                agent_conversation.c.id == conversation_id,
                agent_conversation.c.user_id == user_id,
                agent_conversation.c.active_turn_uid == turn_uid,
            )
            .values(active_turn_uid=None, updated_at=func.now())
        )
        row = (
            await self._session.execute(
                select(agent_message).where(agent_message.c.id == assistant_id)
            )
        ).mappings().one()
        return _message(row)

    async def context_messages(
        self,
        user_id: str,
        conversation_id: int,
        *,
        after_id: int | None,
        through_id: int | None = None,
        limit: int,
    ) -> list[MessageRecord]:
        """读取摘要游标之后最近的有界消息。"""
        filters = [
            agent_message.c.user_id == user_id,
            agent_message.c.conversation_id == conversation_id,
        ]
        if after_id is not None:
            filters.append(agent_message.c.id > after_id)
        if through_id is not None:
            filters.append(agent_message.c.id <= through_id)
        rows = list(
            (
                await self._session.execute(
                    select(agent_message)
                    .where(*filters)
                    .order_by(agent_message.c.id.desc())
                    .limit(limit)
                )
            ).mappings()
        )
        return [_message(row) for row in reversed(rows)]

    async def claim_extractions(
        self,
        *,
        limit: int,
        lease_seconds: int,
        message_limit: int,
    ) -> list[ClaimedExtraction]:
        """短事务领取到期任务并加载同租户有界消息。"""
        earlier = conversation_memory_outbox.alias("earlier_extraction")
        earliest_in_conversation = (
            select(func.min(earlier.c.id))
            .where(
                earlier.c.conversation_id
                == conversation_memory_outbox.c.conversation_id
            )
            .correlate(conversation_memory_outbox)
            .scalar_subquery()
        )
        rows = list(
            (
                await self._session.execute(
                    select(conversation_memory_outbox)
                    .where(
                        conversation_memory_outbox.c.id
                        == earliest_in_conversation,
                        conversation_memory_outbox.c.available_at <= func.now(),
                        or_(
                            conversation_memory_outbox.c.lease_token.is_(None),
                            conversation_memory_outbox.c.lease_expires_at
                            <= func.now(),
                        ),
                    )
                    .order_by(conversation_memory_outbox.c.id)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            ).mappings()
        )
        claimed: list[ClaimedExtraction] = []
        for row in rows:
            token = uuid4().hex
            await self._session.execute(
                update(conversation_memory_outbox)
                .where(conversation_memory_outbox.c.id == int(row["id"]))
                .values(
                    lease_token=token,
                    lease_expires_at=func.timestampadd(
                        text("SECOND"),
                        lease_seconds,
                        func.now(),
                    ),
                )
            )
            conversation = (
                await self._session.execute(
                    select(agent_conversation).where(
                        agent_conversation.c.id == int(row["conversation_id"]),
                        agent_conversation.c.user_id == str(row["user_id"]),
                    )
                )
            ).mappings().one()
            messages = await self.context_messages(
                str(row["user_id"]),
                int(row["conversation_id"]),
                after_id=conversation["summary_through_message_id"],
                through_id=int(row["assistant_message_id"]),
                limit=message_limit,
            )
            claimed.append(
                ClaimedExtraction(
                    outbox_id=int(row["id"]),
                    lease_token=token,
                    attempts=int(row["attempts"]),
                    user_id=str(row["user_id"]),
                    conversation_id=int(row["conversation_id"]),
                    conversation_uid=str(conversation["uid"]),
                    summary=(
                        str(conversation["summary"])
                        if conversation["summary"] is not None
                        else None
                    ),
                    messages=messages,
                )
            )
        return claimed

    async def finish_extraction(
        self,
        claim: ClaimedExtraction,
        summary: str,
    ) -> bool:
        """按 lease token 确认任务并单调推进摘要游标。"""
        current = (
            await self._session.execute(
                select(conversation_memory_outbox.c.id).where(
                    conversation_memory_outbox.c.id == claim.outbox_id,
                    conversation_memory_outbox.c.lease_token == claim.lease_token,
                )
            )
        ).scalar_one_or_none()
        if current is None:
            return False
        through = max((message.id for message in claim.messages), default=0)
        await self._session.execute(
            update(agent_conversation)
            .where(
                agent_conversation.c.id == claim.conversation_id,
                agent_conversation.c.user_id == claim.user_id,
                or_(
                    agent_conversation.c.summary_through_message_id.is_(None),
                    agent_conversation.c.summary_through_message_id < through,
                ),
            )
            .values(
                summary=summary,
                summary_through_message_id=through,
            )
        )
        await self._session.execute(
            delete(conversation_memory_outbox).where(
                conversation_memory_outbox.c.id == claim.outbox_id,
                conversation_memory_outbox.c.lease_token == claim.lease_token,
            )
        )
        return True

    async def retry_extraction(
        self,
        claim: ClaimedExtraction,
        error_type: str,
        max_backoff_seconds: int,
    ) -> None:
        """清除 lease 并为失败提炼设置有界指数退避。"""
        attempts = claim.attempts + 1
        delay = min(2 ** min(attempts, 20), max_backoff_seconds)
        available = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=delay)
        await self._session.execute(
            update(conversation_memory_outbox)
            .where(
                conversation_memory_outbox.c.id == claim.outbox_id,
                conversation_memory_outbox.c.lease_token == claim.lease_token,
            )
            .values(
                attempts=attempts,
                available_at=available,
                lease_token=None,
                lease_expires_at=None,
                last_error_type=error_type[:128],
            )
        )
