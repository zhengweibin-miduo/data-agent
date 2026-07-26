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
from data_agent.errors import DataAgentError
from data_agent.identifiers import stable_id


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
        # 步骤一：生成不可枚举的稳定标识并写入用户归属，创建动作由调用方事务提交。
        uid = stable_id("conversation", user_id, uuid4().hex)
        result = await self._session.execute(
            insert(agent_conversation).values(uid=uid, user_id=user_id)
        )
        identifier = _inserted_id(result, "会话")

        # 步骤二：按新主键和同一用户边界回读数据库时间戳，避免返回应用时钟推测值。
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
        """按会话自增主键倒序执行稳定 keyset 分页读取用户会话。"""
        # 步骤一：把用户归属和排他游标同时下推到 SQL，并多取一行判断是否存在续页。
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

        # 步骤二：只投影请求窗口；下一游标取本页末行主键，保持新增会话不扰动翻页。
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
        # 步骤一：以用户和会话标识组成完整归属条件，避免先读取再做租户过滤。
        statement = select(agent_conversation).where(
            agent_conversation.c.user_id == user_id,
            agent_conversation.c.uid == conversation_uid,
        )

        # 步骤二：事务型调用按需追加行锁，普通读取沿用相同归属条件。
        if for_update:
            statement = statement.with_for_update()
        return (
            (await self._session.execute(statement)).mappings().one_or_none()
        )

    async def delete(self, user_id: str, conversation_uid: str) -> bool:
        """只删除指定用户拥有的会话。"""
        # 步骤一：删除条件包含用户归属，数据库级联仅清理该会话的消息和提炼任务。
        result = await self._session.execute(
            delete(agent_conversation).where(
                agent_conversation.c.user_id == user_id,
                agent_conversation.c.uid == conversation_uid,
            )
        )

        # 步骤二：把受影响行数转换为业务存在性，交由服务层映射统一的未找到错误。
        return bool(getattr(result, "rowcount", 0))

    async def delete_user_conversations(self, user_id: str) -> None:
        """硬删除用户的全部对话数据。"""
        # 步骤一：按用户边界删除会话，依靠外键级联同步清除消息和待提炼任务。
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
        # 步骤一：先验证会话归属，未知会话与跨用户访问统一表现为不存在。
        conversation = await self.get(user_id, conversation_uid)
        if conversation is None:
            return None
        filters = [
            agent_message.c.user_id == user_id,
            agent_message.c.conversation_id == int(conversation["id"]),
        ]
        if before is not None:
            filters.append(agent_message.c.id < before)

        # 步骤二：用递减主键执行排他游标查询，并多取一行判断后续页面。
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

        # 步骤三：截取当前页后恢复时间线正序，同时以页末主键生成稳定续页游标。
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
        # 步骤一：先锁定会话再检查 active_turn_uid，避免并发请求同时通过门禁。
        conversation = await self.get(
            user_id,
            conversation_uid,
            for_update=True,
        )
        if conversation is None:
            raise DataAgentError(
                "conversation_not_found",
                "conversation_turn",
                "会话不存在",
                http_status=404,
            )
        active_turn = conversation["active_turn_uid"]
        if active_turn is not None and str(active_turn) != turn_uid:
            raise DataAgentError(
                "conversation_busy",
                "conversation_turn",
                "会话已有在途轮次",
                http_status=409,
            )

        # 步骤二：在持锁事务内回查用户消息；同一 turn 仅允许相同内容幂等回放，
        # 内容变化必须拒绝，不能覆盖已经成为会话历史的输入。
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
                raise DataAgentError(
                    "idempotency_conflict",
                    "conversation_turn",
                    "相同轮次的用户内容不一致",
                    http_status=409,
                )
            return _message(existing), conversation

        # 步骤三：新消息与 active_turn_uid 在调用方事务内一并写入，确保可见的
        # 用户消息必然占住活动轮次门禁，后续助手完成只能匹配该 turn。
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
        # 步骤一：锁定会话并读取该 turn 的已持久化消息，使启动状态、幂等回放
        # 和活动门禁判断基于同一事务快照。
        conversation = await self.get(
            user_id,
            conversation_uid,
            for_update=True,
        )
        if conversation is None:
            raise DataAgentError(
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
            raise DataAgentError(
                "turn_not_started",
                "conversation_complete",
                "轮次尚未持久化用户消息",
                http_status=409,
            )

        # 步骤二：已有助手消息只接受同内容回放；首次完成则必须仍持有相同
        # active_turn_uid，防止陈旧请求完成已经被其他轮次取代的工作。
        existing = by_role.get(MessageRole.ASSISTANT.value)
        if existing is not None:
            if str(existing["content"]) != content:
                raise DataAgentError(
                    "idempotency_conflict",
                    "conversation_complete",
                    "相同轮次的助手内容不一致",
                    http_status=409,
                )
            return _message(existing)
        if str(conversation["active_turn_uid"] or "") != turn_uid:
            raise DataAgentError(
                "stale_turn",
                "conversation_complete",
                "轮次不是当前在途轮次",
                http_status=409,
            )
        # 步骤三：助手消息、提炼 outbox 与清除 active_turn_uid 必须在调用方的
        # 同一事务中成败一致，任一步失败都不能提前放行下一轮。
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
        # 步骤一：以用户、会话、摘要游标和可选终点共同圈定本次可见消息区间。
        filters = [
            agent_message.c.user_id == user_id,
            agent_message.c.conversation_id == conversation_id,
        ]
        if after_id is not None:
            filters.append(agent_message.c.id > after_id)
        if through_id is not None:
            filters.append(agent_message.c.id <= through_id)

        # 步骤二：先按主键倒序保留最新窗口，再恢复时间线正序供上下文和提炼消费。
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
        # 步骤一：每个会话只锁定最早轮次中当前可领取的任务，以顺序推进摘要；
        # 过期租约可重领，skip_locked 则让并发 worker 继续处理其他会话。
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

        # 步骤二：先为锁定行写入短 lease，再加载摘要与消息窗口；事务提交后
        # 才允许 worker 在无数据库锁的情况下调用外部模型。
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

            # 步骤三：消息窗口严格位于摘要游标之后、截至当前助手消息，并继续
            # 携带该行的 user_id 边界，保证一次 claim 对应可验证的有界证据集。
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
        # 步骤一：完成时以 lease token 做 compare-and-set，阻止任务被重新领取后
        # 的旧 worker 确认结果。
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

        # 步骤二：摘要游标只能向前推进，且摘要更新与 outbox 删除在同一事务
        # 提交；失败时任务仍可由后续 worker 重试。
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
        # 步骤一：退避随尝试次数指数增长但受全局上限约束，避免故障期间形成
        # 高频重试，同时保证任务仍有下一次可领取时间。
        attempts = claim.attempts + 1
        delay = min(2 ** min(attempts, 20), max_backoff_seconds)
        available = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=delay)

        # 步骤二：仅持有当前 lease token 的 worker 能释放租约并登记失败；
        # 已被重领的任务不会被陈旧异常覆盖新的租约状态。
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
