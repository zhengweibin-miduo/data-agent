"""永久 Agent 对话 MySQL 仓储集成测试。"""

from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select, text, update

from conversation.adapters.mysql.extraction import MySQLExtractionCommitter
from conversation.adapters.mysql.user_data import MySQLUserDataEraser
from conversation.application.extraction import (
    validate_extraction_candidates,
)
from conversation.models import (
    ExtractionCandidate,
    ExtractionResult,
)
from conversation.mysql_tables import (
    agent_conversation,
    conversation_memory_outbox,
)
from conversation.repository import ConversationRepository
from errors import DataAgentError
from infrastructure.mysql import MySQLDatabase
from memory.mysql.repository import MemoryRepository
from memory.mysql.tables import (
    agent_memory,
    agent_memory_event,
    agent_memory_link,
    memory_index_outbox,
)
from models.memory import UserMemoryCategory
from persistence.schema import metadata
from settings import app_config
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


async def test_expired_reclaim_fences_every_old_owner_mutation() -> None:
    """A live MySQL reclaim makes old renew, complete, and abandon harmless."""
    engine = MySQLDatabase.initialize()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    user_id = f"conversation-fencing-{uuid4().hex}"
    conversation_uid = ""
    try:
        async with MySQLDatabase.session() as session:
            repository = ConversationRepository(session)
            conversation = await repository.create(user_id)
            conversation_uid = conversation.uid
            _, _, owner, old_token = await repository.start_turn(
                user_id, conversation_uid, "turn-1", "查询问题"
            )
            assert owner and old_token is not None

        async with MySQLDatabase.session() as session:
            await session.execute(
                update(agent_conversation)
                .where(agent_conversation.c.uid == conversation_uid)
                .values(
                    updated_at=func.timestampadd(
                        text("SECOND"),
                        -(app_config.conversation.turn_lease_seconds + 1),
                        func.now(),
                    )
                )
            )

        async with MySQLDatabase.session() as session:
            repository = ConversationRepository(session)
            _, _, owner, new_token = await repository.start_turn(
                user_id, conversation_uid, "turn-1", "查询问题"
            )
            assert owner and new_token is not None and new_token != old_token

        async with MySQLDatabase.session() as session:
            repository = ConversationRepository(session)
            assert not await repository.renew_turn(
                user_id, conversation_uid, "turn-1", old_token
            )
            await repository.abandon_turn(
                user_id, conversation_uid, "turn-1", old_token
            )
            with pytest.raises(DataAgentError, match="轮次不是当前在途轮次"):
                await repository.complete_turn(
                    user_id,
                    conversation_uid,
                    "turn-1",
                    old_token,
                    "旧 owner 结果",
                )

        async with MySQLDatabase.session() as session:
            repository = ConversationRepository(session)
            assert await repository.renew_turn(
                user_id, conversation_uid, "turn-1", new_token
            )
            completed = await repository.complete_turn(
                user_id,
                conversation_uid,
                "turn-1",
                new_token,
                "新 owner 结果",
            )
            assert completed.content == "新 owner 结果"
    finally:
        await _cleanup_user_scope(user_id, conversation_uid)
        await MySQLDatabase.close()


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
            first, _, _, first_claim = await repository.start_turn(
                user_id,
                conversation_uid,
                "turn-1",
                "永久保存这条用户消息",
            )
            repeated, _, _, _ = await repository.start_turn(
                user_id,
                conversation_uid,
                "turn-1",
                "永久保存这条用户消息",
            )
            assistant = await repository.complete_turn(
                user_id,
                conversation_uid,
                "turn-1",
                first_claim or "",
                "已经永久保存",
            )
            repeated_assistant = await repository.complete_turn(
                user_id,
                conversation_uid,
                "turn-1",
                first_claim or "",
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
                _, _, _, claim_token = await repository.start_turn(
                    user_id,
                    conversation_uid,
                    turn,
                    f"{turn} 用户消息",
                )
                await repository.complete_turn(
                    user_id,
                    conversation_uid,
                    turn,
                    claim_token or "",
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


async def test_summary_update_preserves_abandoned_turn_reclaim() -> None:
    """摘要 worker 不得覆盖失败轮次的立即重试标记。"""
    engine = MySQLDatabase.initialize()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    user_id = f"conversation-abandon-summary-{uuid4().hex}"
    conversation_uid = ""
    try:
        async with MySQLDatabase.session() as session:
            repository = ConversationRepository(session)
            conversation = await repository.create(user_id)
            conversation_uid = conversation.uid
            _, _, _, first_claim = await repository.start_turn(
                user_id, conversation_uid, "turn-1", "第一轮"
            )
            await repository.complete_turn(
                user_id, conversation_uid, "turn-1", first_claim or "", "已完成"
            )
            _, _, _, retry_claim = await repository.start_turn(
                user_id, conversation_uid, "turn-2", "重试问题"
            )
            await repository.abandon_turn(
                user_id, conversation_uid, "turn-2", retry_claim or ""
            )

        async with MySQLDatabase.session() as session:
            repository = ConversationRepository(session)
            claims = await repository.claim_extractions(
                limit=10, lease_seconds=180, message_limit=20
            )
            check_equal("待完成摘要数量", len(claims), 1)
            await repository.finish_extraction(claims[0], "第一轮摘要")

        async with MySQLDatabase.session() as session:
            _, _, execution_owner, _ = await ConversationRepository(
                session
            ).start_turn(user_id, conversation_uid, "turn-2", "重试问题")
            check_equal("摘要完成后立即重新认领", execution_owner, True)
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
            user_message, _, _, claim_token = await repository.start_turn(
                user_id,
                conversation_uid,
                "turn-1",
                "我只使用公制单位",
            )
            await repository.complete_turn(
                user_id,
                conversation_uid,
                "turn-1",
                claim_token or "",
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
