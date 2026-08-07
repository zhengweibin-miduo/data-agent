"""Conversation 应用接口与公开提炼 seam 测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from conversation.application.contracts import StartedConversationTurn
from conversation.application.extraction import ConversationMemoryExtractor
from conversation.application.service import ConversationService
from conversation.models import (
    ClaimedExtraction,
    ConversationPage,
    ConversationRecord,
    ExtractionCandidate,
    ExtractionResult,
    MessagePage,
    MessageRecord,
    MessageRole,
)
from models.memory import MemoryCandidate, MemoryDetail, UserMemoryCategory


def _message(identifier: int, role: MessageRole, content: str) -> MessageRecord:
    """构造有稳定时序的消息。"""
    return MessageRecord(
        id=identifier,
        uid=f"message-{identifier}",
        turn_uid=f"turn-{identifier}",
        role=role,
        content=content,
        created_at=datetime.now(UTC),
    )


def _claim(messages: list[MessageRecord]) -> ClaimedExtraction:
    """构造一个已领取提炼任务。"""
    return ClaimedExtraction(
        outbox_id=1,
        lease_token="lease",
        attempts=0,
        user_id="user-a",
        conversation_id=1,
        conversation_uid="conversation-a",
        messages=messages,
    )


class _ConversationStore:
    def __init__(self) -> None:
        self.turn_committed = False

    async def create(self, user_id: str) -> ConversationRecord:
        raise NotImplementedError

    async def list(
        self, user_id: str, *, before: int | None, limit: int
    ) -> ConversationPage:
        raise NotImplementedError

    async def history(
        self,
        user_id: str,
        conversation_uid: str,
        *,
        before: int | None,
        limit: int,
    ) -> MessagePage | None:
        raise NotImplementedError

    async def delete(self, user_id: str, conversation_uid: str) -> bool:
        raise NotImplementedError

    async def start_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        content: str,
        *,
        semantic_fingerprint: str | None = None,
    ) -> StartedConversationTurn:
        del semantic_fingerprint
        self.turn_committed = True
        return StartedConversationTurn(
            message=_message(2, MessageRole.USER, content),
            conversation_id=1,
            summary="旧摘要",
            summary_through_message_id=1,
        )

    async def complete_turn(
        self,
        user_id: str,
        conversation_uid: str,
        turn_uid: str,
        content: str,
        *,
        semantic_fingerprint: str | None = None,
    ) -> MessageRecord:
        raise NotImplementedError

    async def abandon_turn(
        self, user_id: str, conversation_uid: str, turn_uid: str
    ) -> None:
        raise NotImplementedError

    async def renew_turn(
        self, user_id: str, conversation_uid: str, turn_uid: str
    ) -> bool:
        raise NotImplementedError

    async def assistant_message(
        self, user_id: str, conversation_uid: str, turn_uid: str
    ) -> MessageRecord | None:
        raise NotImplementedError

    async def context_messages(
        self,
        user_id: str,
        conversation_id: int,
        *,
        after_id: int | None,
        limit: int,
    ) -> list[MessageRecord]:
        assert after_id == 1
        return [_message(2, MessageRole.USER, "请使用公制")]


class _MemoryReader:
    def __init__(self, store: _ConversationStore) -> None:
        self._store = store
        self.called = False

    async def recall(
        self, query: str, user_id: str, *, limit: int
    ) -> list[MemoryDetail]:
        assert self._store.turn_committed
        assert (query, user_id, limit) == ("请使用公制", "user-a", 4)
        self.called = True
        return []


class _UserDataEraser:
    def __init__(self) -> None:
        self.users: list[str] = []

    async def erase(self, user_id: str) -> None:
        self.users.append(user_id)


@pytest.mark.asyncio
async def test_start_turn_recalls_only_after_authoritative_commit() -> None:
    """验证轮次提交后才读取同用户长期记忆。"""
    store = _ConversationStore()
    reader = _MemoryReader(store)
    service = ConversationService(
        store,
        reader,
        _UserDataEraser(),
        context_message_limit=20,
        context_max_chars=128,
        summary_max_chars=64,
        memory_search_limit=4,
    )

    response = await service.start_turn(
        "user-a", "conversation-a", "turn-a", "请使用公制"
    )

    assert reader.called
    assert response.context.summary == "旧摘要"
    assert [item.content for item in response.context.messages] == ["请使用公制"]


@pytest.mark.asyncio
async def test_delete_user_data_uses_atomic_eraser_seam() -> None:
    """验证用户全量删除只穿过原子 eraser interface。"""
    eraser = _UserDataEraser()
    service = ConversationService(
        _ConversationStore(),
        _MemoryReader(_ConversationStore()),
        eraser,
        context_message_limit=20,
        context_max_chars=128,
        summary_max_chars=64,
        memory_search_limit=4,
    )

    result = await service.delete_user_data("user-a")

    assert result.deleted is True
    assert eraser.users == ["user-a"]


class _ExtractionClaims:
    def __init__(self, claim: ClaimedExtraction) -> None:
        self._claim = claim
        self._returned = False
        self.retries: list[tuple[int, str]] = []

    async def claim(
        self, *, limit: int, lease_seconds: int, message_limit: int
    ) -> list[ClaimedExtraction]:
        if self._returned:
            return []
        self._returned = True
        return [self._claim]

    async def retry(self, claim: ClaimedExtraction, error_type: str) -> None:
        self.retries.append((claim.outbox_id, error_type))


class _ExtractionModel:
    def __init__(self, result: ExtractionResult) -> None:
        self._result = result

    async def extract(self, claim: ClaimedExtraction) -> ExtractionResult:
        return self._result


class _ExtractionCommitter:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.candidates: list[MemoryCandidate] = []

    async def commit(
        self,
        claim: ClaimedExtraction,
        candidates: list[MemoryCandidate],
        summary: str,
    ) -> None:
        if self.fail:
            raise RuntimeError("commit failed")
        self.candidates = candidates


@pytest.mark.asyncio
async def test_extraction_public_seam_commits_only_validated_evidence() -> None:
    """验证公开 dispatch seam 只提交带精确用户原文的候选。"""
    claim = _claim([_message(1, MessageRole.USER, "我只使用公制单位")])
    result = ExtractionResult(
        summary="用户要求使用公制单位。",
        candidates=[
            ExtractionCandidate(
                category=UserMemoryCategory.PREFERENCE,
                key="unit_system",
                value="公制",
                supporting_user_quote="我只使用公制单位",
                evidence_message_uids=["message-1"],
            ),
            ExtractionCandidate(
                category=UserMemoryCategory.PROFILE,
                key="country",
                value="中国",
                supporting_user_quote="用户来自中国",
                evidence_message_uids=["message-1"],
            ),
        ],
    )
    claims = _ExtractionClaims(claim)
    committer = _ExtractionCommitter()
    extractor = ConversationMemoryExtractor(
        _ExtractionModel(result),
        claims,
        committer,
        batch_size=1,
        max_concurrency=1,
        lease_seconds=180,
        message_limit=20,
        summary_max_chars=4096,
        content_version="v1",
        projection_version="v1",
    )

    processed = await extractor.dispatch()

    assert processed == 1
    assert [candidate.memory_key for candidate in committer.candidates] == [
        "unit_system"
    ]
    assert claims.retries == []


@pytest.mark.asyncio
async def test_extraction_commit_failure_releases_claim_for_retry() -> None:
    """验证原子提交失败后保留提炼任务并登记退避。"""
    claim = _claim([_message(1, MessageRole.USER, "我只使用公制单位")])
    claims = _ExtractionClaims(claim)
    extractor = ConversationMemoryExtractor(
        _ExtractionModel(ExtractionResult(summary="", candidates=[])),
        claims,
        _ExtractionCommitter(fail=True),
        batch_size=1,
        max_concurrency=1,
        lease_seconds=180,
        message_limit=20,
        summary_max_chars=4096,
        content_version="v1",
        projection_version="v1",
    )

    processed = await extractor.dispatch()

    assert processed == 0
    assert claims.retries == [(1, "RuntimeError")]
