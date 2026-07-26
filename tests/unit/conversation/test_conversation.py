"""Agent 对话契约和证据校验测试。"""

from datetime import UTC, datetime

from fastapi.routing import APIRoute
from pydantic import ValidationError

from data_agent.conversation.api import router
from data_agent.conversation.extraction import _validated_candidates
from data_agent.conversation.models import (
    ClaimedExtraction,
    ExtractionCandidate,
    ExtractionResult,
    MessageRecord,
    MessageRole,
    StartTurnRequest,
)
from data_agent.models.memory import (
    UserMemoryCategory,
    UserMemoryContent,
)
from tests.helpers.checks import check_equal, check_exception, fail_check


def _message(
    identifier: int,
    role: MessageRole,
    content: str,
) -> MessageRecord:
    """构造有稳定顺序的测试消息。"""
    return MessageRecord(
        id=identifier,
        uid=f"message-{identifier}",
        turn_uid=f"turn-{identifier}",
        role=role,
        content=content,
        created_at=datetime.now(UTC),
    )


def _claim(messages: list[MessageRecord]) -> ClaimedExtraction:
    """构造单个已领取提炼任务。"""
    return ClaimedExtraction(
        outbox_id=1,
        lease_token="lease",
        attempts=0,
        user_id="user-a",
        conversation_id=1,
        conversation_uid="conversation-a",
        messages=messages,
    )


def _claim_for_user(
    user_id: str,
    messages: list[MessageRecord],
) -> ClaimedExtraction:
    """构造指定用户的提炼任务。"""
    return _claim(messages).model_copy(update={"user_id": user_id})


def test_turn_contract_rejects_non_text_fields_and_oversized_text() -> None:
    """验证未知载荷和超长文本在 Pydantic 边界被拒绝。"""
    cases = (
        {
            "user_id": "user-a",
            "turn_uid": "turn-a",
            "content": "hello",
            "attachment": {"name": "secret.bin"},
        },
        {
            "user_id": "user-a",
            "turn_uid": "turn-a",
            "content": "x" * 32769,
        },
    )
    for payload in cases:
        try:
            StartTurnRequest.model_validate(payload)
        except ValidationError as error:
            check_exception("非法对话载荷", error, ValidationError)
        else:
            fail_check(
                "非法对话载荷",
                actual=payload,
                expected="Pydantic ValidationError",
            )


def test_conversation_route_contract() -> None:
    """锁定永久会话和用户记忆路由。"""
    routes = {
        (route.path, tuple(sorted(route.methods or set())))
        for route in router.routes
        if isinstance(route, APIRoute)
    }
    check_equal(
        "对话 API 路由",
        routes,
        {
            ("/api/v1/conversations", ("GET",)),
            ("/api/v1/conversations", ("POST",)),
            (
                "/api/v1/conversations/{conversation_uid}/messages",
                ("GET",),
            ),
            (
                "/api/v1/conversations/{conversation_uid}",
                ("DELETE",),
            ),
            (
                "/api/v1/conversations/{conversation_uid}/turns",
                ("POST",),
            ),
            (
                "/api/v1/conversations/{conversation_uid}/turns/{turn_uid}/assistant",
                ("POST",),
            ),
            ("/api/v1/users/{user_id}/memories/search", ("GET",)),
            (
                "/api/v1/users/{user_id}/memories/{memory_uid}",
                ("GET",),
            ),
            (
                "/api/v1/users/{user_id}/memories/{memory_uid}/history",
                ("GET",),
            ),
            (
                "/api/v1/users/{user_id}/memories/{memory_uid}",
                ("PATCH",),
            ),
            (
                "/api/v1/users/{user_id}/memories/{memory_uid}",
                ("DELETE",),
            ),
            (
                "/api/v1/users/{user_id}/conversation-data",
                ("DELETE",),
            ),
        },
    )


def test_extraction_requires_exact_user_quote() -> None:
    """验证只有用户消息中的精确原文可以形成长期记忆。"""
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
    accepted = _validated_candidates(claim, result)
    check_equal("精确用户原文候选数量", len(accepted), 1)
    check_equal("精确用户原文候选键", accepted[0].memory_key, "unit_system")
    check_equal("精确用户原文候选类别", accepted[0].category, "user.preference")


def test_extraction_rejects_value_not_supported_by_quote() -> None:
    """验证真实原文不能为模型捏造的另一项事实背书。"""
    claim = _claim([_message(1, MessageRole.USER, "我只使用公制单位")])
    result = ExtractionResult(
        summary="",
        candidates=[
            ExtractionCandidate(
                category=UserMemoryCategory.PREFERENCE,
                key="unit_system",
                value="英制",
                supporting_user_quote="我只使用公制单位",
                evidence_message_uids=["message-1"],
            )
        ],
    )
    check_equal("原文不支持候选值", _validated_candidates(claim, result), [])


def test_extraction_rejects_assistant_guess_and_ambiguous_confirmation() -> None:
    """验证助手单方结论和模糊确认都不能形成长期记忆。"""
    claim = _claim(
        [
            _message(1, MessageRole.ASSISTANT, "你应该偏好简短回答"),
            _message(2, MessageRole.USER, "好的"),
        ]
    )
    result = ExtractionResult(
        summary="",
        candidates=[
            ExtractionCandidate(
                category=UserMemoryCategory.PREFERENCE,
                key="answer_style",
                value="简短",
                supporting_user_quote="好的",
                evidence_message_uids=["message-2"],
                confirmed_assistant_message_uid="message-1",
                assistant_quote="你应该偏好简短回答",
            )
        ],
    )
    check_equal(
        "模糊确认候选",
        _validated_candidates(claim, result),
        [],
    )


def test_extraction_accepts_explicit_later_confirmation() -> None:
    """验证用户后续明确确认的助手结论可以成为记忆。"""
    claim = _claim(
        [
            _message(1, MessageRole.ASSISTANT, "默认币种是人民币"),
            _message(2, MessageRole.USER, "我确认默认币种是人民币"),
        ]
    )
    result = ExtractionResult(
        summary="用户确认默认币种。",
        candidates=[
            ExtractionCandidate(
                category=UserMemoryCategory.BUSINESS_RULE,
                key="default_currency",
                value="人民币",
                supporting_user_quote="我确认默认币种是人民币",
                evidence_message_uids=["message-2"],
                confirmed_assistant_message_uid="message-1",
                assistant_quote="默认币种是人民币",
            )
        ],
    )
    accepted = _validated_candidates(claim, result)
    check_equal("明确确认候选数量", len(accepted), 1)
    content = accepted[0].content
    check_equal(
        "明确确认助手消息",
        (
            content.confirmed_assistant_message_uid
            if isinstance(content, UserMemoryContent)
            else None
        ),
        "message-1",
    )


def test_extraction_rejects_unrelated_later_statement() -> None:
    """验证后续用户原文必须明确复述所确认的助手结论。"""
    claim = _claim(
        [
            _message(1, MessageRole.ASSISTANT, "默认币种是人民币"),
            _message(2, MessageRole.USER, "我确认默认时区是 UTC"),
        ]
    )
    result = ExtractionResult(
        summary="",
        candidates=[
            ExtractionCandidate(
                category=UserMemoryCategory.BUSINESS_RULE,
                key="default_currency",
                value="人民币",
                supporting_user_quote="我确认默认时区是 UTC",
                evidence_message_uids=["message-2"],
                confirmed_assistant_message_uid="message-1",
                assistant_quote="默认币种是人民币",
            )
        ],
    )
    check_equal("无关后续陈述候选", _validated_candidates(claim, result), [])


def test_user_memory_uid_includes_tenant_scope() -> None:
    """验证相同事实在不同用户下生成不同权威 UID。"""
    message = _message(1, MessageRole.USER, "我只使用公制单位")
    result = ExtractionResult(
        summary="",
        candidates=[
            ExtractionCandidate(
                category=UserMemoryCategory.PREFERENCE,
                key="unit_system",
                value="公制",
                supporting_user_quote="我只使用公制单位",
                evidence_message_uids=["message-1"],
            )
        ],
    )
    first = _validated_candidates(
        _claim_for_user("user-a", [message]),
        result,
    )[0]
    second = _validated_candidates(
        _claim_for_user("user-b", [message]),
        result,
    )[0]
    check_equal("不同用户 UID 是否不同", first.uid == second.uid, False)


def test_extraction_keeps_one_value_per_user_scope() -> None:
    """验证单次模型输出不能产生两个同作用域活动值。"""
    claim = _claim([_message(1, MessageRole.USER, "我偏好红色，不再偏好蓝色")])
    result = ExtractionResult(
        summary="",
        candidates=[
            ExtractionCandidate(
                category=UserMemoryCategory.PREFERENCE,
                key="color",
                value="红色",
                supporting_user_quote="我偏好红色",
                evidence_message_uids=["message-1"],
            ),
            ExtractionCandidate(
                category=UserMemoryCategory.PREFERENCE,
                key="color",
                value="蓝色",
                supporting_user_quote="偏好蓝色",
                evidence_message_uids=["message-1"],
            ),
        ],
    )
    accepted = _validated_candidates(claim, result)
    check_equal("同作用域候选数量", len(accepted), 1)
    content = accepted[0].content
    check_equal(
        "同作用域首个值",
        content.value if isinstance(content, UserMemoryContent) else None,
        "红色",
    )
