"""永久 Agent 对话 MySQL 仓储集成测试。"""

from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from data_agent.conversation.adapters.mysql.extraction import MySQLExtractionCommitter
from data_agent.conversation.adapters.mysql.user_data import MySQLUserDataEraser
from data_agent.conversation.application.extraction import (
    validate_extraction_candidates,
)
from data_agent.conversation.models import (
    ExtractionCandidate,
    ExtractionResult,
)
from data_agent.conversation.mysql_tables import (
    agent_conversation,
    conversation_memory_outbox,
)
from data_agent.conversation.repository import ConversationRepository
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.memory.mysql.repository import MemoryRepository
from data_agent.memory.mysql.tables import (
    agent_memory,
    agent_memory_event,
    agent_memory_link,
    memory_index_outbox,
)
from data_agent.models.memory import UserMemoryCategory
from data_agent.persistence.schema import metadata
from tests.helpers.checks import check_equal

pytestmark = pytest.mark.integration


async def _cleanup_user_scope(user_id: str, conversation_uid: str) -> None:
    """按 UUID 测试租户清理 Conversation 与 Long-term Memory 数据。"""
    async with MySQLDatabase.session() as session:
        memory_ids = set(
            (
                await session.scalars(
                    select(agent_memory.c.id).where(agent_memory.c.user_id == user_id)
                )
            ).all()
        )
        memory_uids = set(
            (
                await session.scalars(
                    select(agent_memory.c.uid).where(agent_memory.c.user_id == user_id)
                )
            ).all()
        )
        if memory_ids:
            await session.execute(
                delete(agent_memory_link).where(
                    (agent_memory_link.c.memory_id.in_(memory_ids))
                    | (agent_memory_link.c.linked_memory_id.in_(memory_ids))
                )
            )
            await session.execute(
                delete(agent_memory_event).where(
                    agent_memory_event.c.memory_id.in_(memory_ids)
                )
            )
        if memory_uids:
            await session.execute(
                delete(memory_index_outbox).where(
                    memory_index_outbox.c.memory_uid.in_(memory_uids)
                )
            )
        if memory_ids:
            await session.execute(
                delete(agent_memory).where(agent_memory.c.id.in_(memory_ids))
            )
        if conversation_uid:
            await session.execute(
                delete(agent_conversation).where(
                    agent_conversation.c.uid == conversation_uid
                )
            )


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


async def test_user_data_eraser_rolls_back_conversation_when_memory_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证 Memory tombstone 失败会回滚此前的 Conversation 删除。"""
    engine = MySQLDatabase.initialize()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    user_id = f"conversation-erase-{uuid4().hex}"
    conversation_uid = ""
    try:
        async with MySQLDatabase.session() as session:
            conversation = await ConversationRepository(session).create(user_id)
            conversation_uid = conversation.uid

        async def fail_tombstone(_repository: MemoryRepository, _user_id: str) -> None:
            raise RuntimeError("forced memory failure")

        monkeypatch.setattr(MemoryRepository, "tombstone_user", fail_tombstone)
        with pytest.raises(RuntimeError, match="forced memory failure"):
            await MySQLUserDataEraser().erase(user_id)

        async with MySQLDatabase.session() as session:
            page = await ConversationRepository(session).history(
                user_id,
                conversation_uid,
                before=None,
                limit=10,
            )
        assert page is not None
    finally:
        await _cleanup_user_scope(user_id, conversation_uid)
        await MySQLDatabase.close()


async def test_extraction_committer_rolls_back_memory_when_finish_loses_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证提炼完成 CAS 失败会回滚同事务内的 Memory candidate。"""
    engine = MySQLDatabase.initialize()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    user_id = f"conversation-extraction-{uuid4().hex}"
    conversation_uid = ""
    candidate_uid = ""
    try:
        async with MySQLDatabase.session() as session:
            repository = ConversationRepository(session)
            conversation = await repository.create(user_id)
            conversation_uid = conversation.uid
            user_message, _ = await repository.start_turn(
                user_id,
                conversation_uid,
                "turn-1",
                "我只使用公制单位",
            )
            await repository.complete_turn(
                user_id,
                conversation_uid,
                "turn-1",
                "以后会使用公制单位",
            )

        async with MySQLDatabase.session() as session:
            claims = await ConversationRepository(session).claim_extractions(
                limit=1,
                lease_seconds=180,
                message_limit=20,
            )
        assert len(claims) == 1
        claim = claims[0]
        candidates = validate_extraction_candidates(
            claim,
            ExtractionResult(
                summary="用户要求使用公制单位。",
                candidates=[
                    ExtractionCandidate(
                        category=UserMemoryCategory.PREFERENCE,
                        key="unit_system",
                        value="公制",
                        supporting_user_quote="我只使用公制单位",
                        evidence_message_uids=[user_message.uid],
                    )
                ],
            ),
        )
        assert len(candidates) == 1
        candidate_uid = candidates[0].uid

        async def lose_lease(
            _repository: ConversationRepository,
            _claim: object,
            _summary: str,
        ) -> bool:
            return False

        monkeypatch.setattr(ConversationRepository, "finish_extraction", lose_lease)
        with pytest.raises(RuntimeError, match="提炼 lease 已失效"):
            await MySQLExtractionCommitter().commit(
                claim,
                candidates,
                "用户要求使用公制单位。",
            )

        async with MySQLDatabase.session() as session:
            memory = await MemoryRepository(session).get_by_uid(
                candidate_uid,
                user_id=user_id,
            )
            outbox_exists = await session.scalar(
                select(func.count())
                .select_from(conversation_memory_outbox)
                .where(conversation_memory_outbox.c.id == claim.outbox_id)
            )
        assert memory is None
        assert outbox_exists == 1
    finally:
        await _cleanup_user_scope(user_id, conversation_uid)
        await MySQLDatabase.close()
