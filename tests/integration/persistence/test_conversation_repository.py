"""永久 Agent 对话 MySQL 仓储集成测试。"""

from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from data_agent.conversation.mysql_tables import (
    agent_conversation,
    conversation_memory_outbox,
)
from data_agent.conversation.repository import ConversationRepository
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.persistence.schema import metadata
from tests.helpers.checks import check_equal

pytestmark = pytest.mark.integration


async def test_turn_idempotency_history_and_tenant_isolation() -> None:
    """验证消息、提炼 outbox、稳定历史和用户隔离。"""
    engine = MySQLDatabase.initialize()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    user_id = f"conversation-test-{uuid4().hex}"
    other_user = f"conversation-other-{uuid4().hex}"
    conversation_uid = ""
    try:
        async with MySQLDatabase.session() as session:
            repository = ConversationRepository(session)
            conversation = await repository.create(user_id)
            conversation_uid = conversation.uid
            first, _ = await repository.start_turn(
                user_id,
                conversation_uid,
                "turn-1",
                "永久保存这条用户消息",
            )
            repeated, _ = await repository.start_turn(
                user_id,
                conversation_uid,
                "turn-1",
                "永久保存这条用户消息",
            )
            assistant = await repository.complete_turn(
                user_id,
                conversation_uid,
                "turn-1",
                "已经永久保存",
            )
            repeated_assistant = await repository.complete_turn(
                user_id,
                conversation_uid,
                "turn-1",
                "已经永久保存",
            )
            check_equal("用户消息幂等", repeated.uid, first.uid)
            check_equal("助手消息幂等", repeated_assistant.uid, assistant.uid)

        async with MySQLDatabase.session() as session:
            repository = ConversationRepository(session)
            history = await repository.history(
                user_id,
                conversation_uid,
                before=None,
                limit=10,
            )
            hidden = await repository.history(
                other_user,
                conversation_uid,
                before=None,
                limit=10,
            )
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(conversation_memory_outbox)
                .join(
                    agent_conversation,
                    agent_conversation.c.id
                    == conversation_memory_outbox.c.conversation_id,
                )
                .where(agent_conversation.c.uid == conversation_uid)
            )
            check_equal(
                "消息顺序",
                [message.role.value for message in history.items] if history else [],
                ["user", "assistant"],
            )
            check_equal("其他用户不可见", hidden, None)
            check_equal("提炼 outbox 幂等", outbox_count, 1)
    finally:
        if conversation_uid:
            async with MySQLDatabase.session() as session:
                await session.execute(
                    delete(agent_conversation).where(
                        agent_conversation.c.uid == conversation_uid
                    )
                )
        await MySQLDatabase.close()


async def test_extraction_claims_one_ordered_turn_per_conversation() -> None:
    """验证摘要提炼不会并行领取同一会话的后续轮次。"""
    engine = MySQLDatabase.initialize()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    user_id = f"conversation-claim-{uuid4().hex}"
    conversation_uid = ""
    try:
        async with MySQLDatabase.session() as session:
            repository = ConversationRepository(session)
            conversation = await repository.create(user_id)
            conversation_uid = conversation.uid
            for turn in ("turn-1", "turn-2"):
                await repository.start_turn(
                    user_id,
                    conversation_uid,
                    turn,
                    f"{turn} 用户消息",
                )
                await repository.complete_turn(
                    user_id,
                    conversation_uid,
                    turn,
                    f"{turn} 助手消息",
                )

        async with MySQLDatabase.session() as session:
            repository = ConversationRepository(session)
            first = await repository.claim_extractions(
                limit=10,
                lease_seconds=180,
                message_limit=20,
            )
            check_equal("同会话首次领取数量", len(first), 1)
            check_equal(
                "首次领取轮次",
                {message.turn_uid for message in first[0].messages},
                {"turn-1"},
            )
            await repository.finish_extraction(first[0], "第一轮摘要")

        async with MySQLDatabase.session() as session:
            second = await ConversationRepository(session).claim_extractions(
                limit=10,
                lease_seconds=180,
                message_limit=20,
            )
            check_equal("同会话后续领取数量", len(second), 1)
            check_equal("后续领取继承摘要", second[0].summary, "第一轮摘要")
            check_equal(
                "后续领取轮次",
                {message.turn_uid for message in second[0].messages},
                {"turn-2"},
            )
    finally:
        if conversation_uid:
            async with MySQLDatabase.session() as session:
                await session.execute(
                    delete(agent_conversation).where(
                        agent_conversation.c.uid == conversation_uid
                    )
                )
        await MySQLDatabase.close()
