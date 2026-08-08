"""当前 DDL 上下文聊天编排测试。"""

import asyncio
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi.routing import APIRoute
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from openai import APITimeoutError

from answer_readiness.models import (
    AnswerGateDecision,
    AnswerGateResult,
    AnswerTargetCatalog,
)
from answer_readiness.service import AnswerReadinessService
from chat.api import router
from chat.models import ChatTurnRequest
from chat.service import ChatService
from conversation.application.service import ConversationService
from conversation.models import (
    CompleteTurnResponse,
    ContextMessage,
    ConversationContext,
    MessageRecord,
    MessageRole,
    StartTurnResponse,
)
from errors import DataAgentError
from models.jobs import DDLJobRequest
from settings import app_config
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)


def _message(role: MessageRole, content: str) -> MessageRecord:
    """构造一条聊天测试消息。"""
    return MessageRecord(
        id=1 if role == MessageRole.USER else 2,
        uid=f"message-{role.value}",
        turn_uid="turn-1",
        role=role,
        content=content,
        created_at=datetime.now(UTC),
    )


def _request() -> ChatTurnRequest:
    """构造当前数据来源和 DDL 的聊天请求。"""
    return ChatTurnRequest(
        user_id="user-1",
        turn_uid="turn-1",
        content="订单金额应该怎样定义？",
        ddl_context=DDLJobRequest(
            source="erp",
            ddl="CREATE TABLE orders (id BIGINT PRIMARY KEY, total DECIMAL(10,2))",
        ),
    )


def _service(
    *,
    decision: AnswerGateDecision = AnswerGateDecision.PROCEED,
    user_message: str | None = None,
    existing: MessageRecord | None = None,
    model_result: object | BaseException = AIMessage(content="按支付成功金额定义。"),
    completion_error: BaseException | None = None,
) -> tuple[ChatService, Mock, Mock, Mock]:
    """用最小异步替身装配聊天服务。"""
    conversations = Mock(spec=ConversationService)
    conversations.start_turn = AsyncMock(
        return_value=StartTurnResponse(
            message=_message(MessageRole.USER, "订单金额应该怎样定义？"),
            context=ConversationContext(
                summary="用户正在定义订单口径。",
                messages=[
                    ContextMessage(
                        role=MessageRole.USER,
                        content="订单金额应该怎样定义？",
                    )
                ],
                memories=[],
            ),
            claim_token="c" * 32,
        )
    )
    conversations.assistant_message = AsyncMock(return_value=existing)
    conversations.complete_turn = AsyncMock(
        side_effect=completion_error,
        return_value=CompleteTurnResponse(
            message=_message(MessageRole.ASSISTANT, "按支付成功金额定义。")
        ),
    )
    conversations.abandon_turn = AsyncMock()
    conversations.renew_turn = AsyncMock(return_value=True)
    readiness = Mock(spec=AnswerReadinessService)
    readiness.evaluate = AsyncMock(
        return_value=AnswerGateResult(
            decision=decision,
            user_message=user_message,
        )
    )
    model = Mock(spec=BaseChatModel)
    model.ainvoke = (
        AsyncMock(side_effect=model_result)
        if isinstance(model_result, BaseException)
        else AsyncMock(return_value=model_result)
    )
    service = ChatService(
        cast(ConversationService, conversations),
        cast(AnswerReadinessService, readiness),
        cast(BaseChatModel, model),
        turn_lease_seconds=0.03,
    )
    return service, conversations, readiness, model


async def test_chat_turn_renews_claim_during_slow_model_call() -> None:
    """健康 Chat 在长模型调用期间持续续租当前 claim。"""
    release = asyncio.Event()

    async def slow_model(*_args: object, **_kwargs: object) -> AIMessage:
        await release.wait()
        return AIMessage(content="按支付成功金额定义。")

    service, conversations, _, model = _service()
    model.ainvoke = AsyncMock(side_effect=slow_model)

    task = asyncio.create_task(service.run_turn("conversation-1", _request()))
    for _ in range(20):
        if conversations.renew_turn.await_count:
            break
        await asyncio.sleep(0.01)
    assert conversations.renew_turn.await_count >= 1
    release.set()
    await task


async def test_chat_turn_stops_when_claim_renewal_is_lost() -> None:
    """Chat claim 续租 CAS 失败时 fence 旧执行者并返回稳定错误。"""
    service, conversations, _, model = _service()
    conversations.renew_turn = AsyncMock(return_value=False)

    async def slow_model(*_args: object, **_kwargs: object) -> AIMessage:
        await asyncio.sleep(1)
        return AIMessage(content="不会完成")

    model.ainvoke = AsyncMock(side_effect=slow_model)

    with pytest.raises(DataAgentError) as caught:
        await service.run_turn("conversation-1", _request())

    assert caught.value.code == "chat_lease_lost"


async def test_chat_turn_ignores_transient_claim_renewal_error() -> None:
    """续租传输异常不能被误判为 claim 已经被替代。"""
    service, conversations, _, model = _service()
    conversations.renew_turn = AsyncMock(
        side_effect=[RuntimeError("temporary database failure"), True]
    )

    async def slow_model(*_args: object, **_kwargs: object) -> AIMessage:
        await asyncio.sleep(0.04)
        return AIMessage(content="按支付成功金额定义。")

    model.ainvoke = AsyncMock(side_effect=slow_model)

    result = await service.run_turn("conversation-1", _request())

    assert result.message.content == "按支付成功金额定义。"
    assert conversations.renew_turn.await_count >= 2


async def test_chat_turn_stops_heartbeat_before_completion() -> None:
    """完成事务开始前必须停止 heartbeat，避免成功提交后自我 fence。"""
    service, conversations, _, _ = _service()

    async def complete(*_args: object) -> CompleteTurnResponse:
        await asyncio.sleep(0.04)
        return CompleteTurnResponse(
            message=_message(MessageRole.ASSISTANT, "按支付成功金额定义。")
        )

    conversations.complete_turn = AsyncMock(side_effect=complete)

    result = await service.run_turn("conversation-1", _request())

    assert result.message.content == "按支付成功金额定义。"
    assert conversations.renew_turn.await_count == 0


async def test_chat_turn_reuses_context_readiness_and_shared_model() -> None:
    """成功轮次携带当前 DDL 上下文并只持久化一次助手消息。"""
    service, conversations, readiness, model = _service()

    result = await service.run_turn("conversation-1", _request())

    check_equal("聊天成功决策", result.readiness, AnswerGateDecision.PROCEED)
    check_equal("聊天成功助手文本", result.message.content, "按支付成功金额定义。")
    catalog = cast(AnswerTargetCatalog, readiness.evaluate.await_args.args[1])
    check_equal("就绪目录表", catalog.targets[0].target_table, "orders")
    check_equal("就绪目录来源", catalog.targets[0].sources, ["erp"])
    prompt = model.ainvoke.await_args.args[0]
    check_condition(
        "模型提示包含当前 DDL",
        "CREATE TABLE orders" in str(prompt[1].content),
    )
    check_condition("模型提示包含当前来源", '"source": "erp"' in str(prompt[1].content))
    check_equal("当前用户消息角色", type(prompt[-1]), HumanMessage)
    check_equal("助手完成次数", conversations.complete_turn.await_count, 1)


def test_chat_route_contract() -> None:
    """锁定独立且直观的服务端聊天编排入口。"""
    routes = {
        (route.path, tuple(sorted(route.methods or set())))
        for route in router.routes
        if isinstance(route, APIRoute)
    }
    check_equal(
        "聊天 API 路由",
        routes,
        {
            (
                "/api/v1/conversations/{conversation_uid}/chat-turns",
                ("POST",),
            )
        },
    )


async def test_chat_turn_persists_fixed_not_ready_message_without_answer_call() -> None:
    """数据未就绪时保存固定安全文案且不调用回答模型。"""
    service, conversations, _, model = _service(
        decision=AnswerGateDecision.DATA_PREPARING,
        user_message="数据准备中，请稍后重试",
    )

    result = await service.run_turn("conversation-1", _request())

    check_equal(
        "未就绪固定文案",
        conversations.complete_turn.await_args.args[4],
        "数据准备中，请稍后重试",
    )
    check_equal("未就绪不调用回答模型", model.ainvoke.await_count, 0)
    check_equal("未就绪响应决策", result.readiness, AnswerGateDecision.DATA_PREPARING)


async def test_chat_turn_replay_returns_existing_assistant_without_model_call() -> None:
    """已完成 turn_uid 回放不重复调用 readiness、模型或完成事务。"""
    existing = _message(MessageRole.ASSISTANT, "已有回答")
    service, conversations, readiness, model = _service(existing=existing)

    result = await service.run_turn("conversation-1", _request())

    check_equal("回放助手消息", result.message, existing)
    check_equal("回放不调用就绪门禁", readiness.evaluate.await_count, 0)
    check_equal("回放不调用模型", model.ainvoke.await_count, 0)
    check_equal("回放不重复完成", conversations.complete_turn.await_count, 0)


async def test_chat_turn_replay_preserves_not_ready_decision() -> None:
    """固定未就绪回复的幂等回放保留原 readiness 决策。"""
    existing = _message(MessageRole.ASSISTANT, "数据准备中，请稍后重试")
    service, _, _, _ = _service(existing=existing)

    result = await service.run_turn("conversation-1", _request())

    check_equal(
        "未就绪回放决策",
        result.readiness,
        AnswerGateDecision.DATA_PREPARING,
    )


async def test_chat_turn_projects_retryable_model_failure() -> None:
    """模型超时投影为稳定可重试业务错误。"""
    timeout = APITimeoutError(request=httpx.Request("POST", "http://model.test"))
    service, _, _, _ = _service(model_result=timeout)

    try:
        await service.run_turn("conversation-1", _request())
    except DataAgentError as error:
        check_exception("模型失败类型", error, DataAgentError)
        check_equal("模型失败代码", error.code, "chat_model_failed")
        check_equal("模型失败可重试", error.retryable, True)
        check_equal("模型失败状态码", error.http_status, 502)
    else:
        fail_check(
            "模型失败投影",
            actual="未抛异常",
            expected="DataAgentError",
        )


async def test_chat_turn_rejects_oversized_ddl_before_starting_turn() -> None:
    """过大 DDL 在占用会话活动轮次前拒绝。"""
    service, conversations, _, _ = _service()
    request = _request().model_copy(
        update={
            "ddl_context": DDLJobRequest(
                source="erp",
                ddl="x" * (app_config.api.max_ddl_bytes + 1),
            )
        }
    )

    try:
        await service.run_turn("conversation-1", request)
    except DataAgentError as error:
        check_equal("聊天 DDL 超限代码", error.code, "ddl_too_large")
        check_equal("聊天 DDL 超限状态", error.http_status, 422)
        check_equal("超限不占用活动轮次", conversations.start_turn.await_count, 0)
    else:
        fail_check(
            "聊天 DDL 超限",
            actual="未抛异常",
            expected="DataAgentError",
        )


async def test_chat_turn_preserves_completion_failure() -> None:
    """助手消息持久化失败时保留原始事务异常。"""
    service, conversations, _, _ = _service(
        completion_error=RuntimeError("write failed")
    )

    try:
        await service.run_turn("conversation-1", _request())
    except RuntimeError as error:
        check_exception("完成轮次失败类型", error, RuntimeError)
        check_equal("完成轮次失败消息", str(error), "write failed")
    else:
        fail_check(
            "完成轮次失败",
            actual="未抛异常",
            expected="RuntimeError",
        )
    check_equal("完成失败释放执行权", conversations.abandon_turn.await_count, 1)


async def test_chat_turn_releases_execution_owner_after_model_failure() -> None:
    """模型失败后释放当前轮次，使同一 turn_uid 可安全重试。"""
    timeout = APITimeoutError(request=httpx.Request("POST", "http://model.test"))
    service, conversations, _, _ = _service(model_result=timeout)

    try:
        await service.run_turn("conversation-1", _request())
    except DataAgentError:
        pass

    check_equal("模型失败释放执行权", conversations.abandon_turn.await_count, 1)


async def test_chat_turn_releases_owner_when_assistant_replay_read_fails() -> None:
    """取得执行权后的助手消息回读失败必须立即释放轮次。"""
    service, conversations, _, _ = _service()
    conversations.assistant_message = AsyncMock(side_effect=RuntimeError("read failed"))

    with pytest.raises(RuntimeError, match="read failed"):
        await service.run_turn("conversation-1", _request())

    conversations.abandon_turn.assert_awaited_once_with(
        "user-1", "conversation-1", "turn-1", "c" * 32
    )


async def test_chat_turn_releases_owner_when_assistant_replay_is_cancelled() -> None:
    """助手消息回读取消也必须释放已经取得的轮次。"""
    service, conversations, _, _ = _service()
    conversations.assistant_message = AsyncMock(side_effect=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await service.run_turn("conversation-1", _request())

    conversations.abandon_turn.assert_awaited_once_with(
        "user-1", "conversation-1", "turn-1", "c" * 32
    )
