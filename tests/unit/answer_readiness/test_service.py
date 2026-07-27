"""回答数据就绪确定性路由检查。"""

from typing import cast
from unittest.mock import AsyncMock, Mock

from langchain_core.tools import BaseTool

from data_agent.answer_readiness.classifier import AnswerReadinessClassifier
from data_agent.answer_readiness.models import (
    AnswerDataDependency,
    AnswerDataTarget,
    AnswerGateDecision,
    AnswerReadinessIntent,
    AnswerTargetCatalog,
)
from data_agent.answer_readiness.service import (
    DATA_PREPARING_MESSAGE,
    INTENT_UNRESOLVED_MESSAGE,
    AnswerReadinessService,
)
from tests.helpers.checks import check_equal, fail_check


def _catalog() -> AnswerTargetCatalog:
    """返回服务测试目录。"""
    return AnswerTargetCatalog(
        targets=[AnswerDataTarget(target_table="orders", sources=["erp"])]
    )


def _service(
    intent: AnswerReadinessIntent | None,
    *,
    ready: bool = True,
) -> tuple[AnswerReadinessService, AsyncMock]:
    """返回绑定确定性识别和工具替身的服务。"""
    classifier = Mock(spec=AnswerReadinessClassifier)
    classifier.classify = AsyncMock(return_value=intent)
    tool = Mock(spec=BaseTool)
    tool.ainvoke = AsyncMock(return_value={"ready": ready})
    service = AnswerReadinessService(
        cast(AnswerReadinessClassifier, classifier),
        cast(BaseTool, tool),
    )
    return service, tool.ainvoke


async def test_no_wait_intent_proceeds_without_tool_call() -> None:
    """不依赖 DW 的问题直接放行且不查询数据库。"""
    service, invoke = _service(
        AnswerReadinessIntent(
            requires_sync_completion=False,
            dependencies=[],
            reason="不依赖 DW。",
        )
    )
    result = await service.evaluate("解释主键", _catalog())
    check_equal("无等待门禁", result.decision, AnswerGateDecision.PROCEED)
    check_equal("无等待不调用工具", invoke.await_count, 0)


async def test_ready_dependencies_proceed() -> None:
    """全部依赖就绪时放行未来业务回答。"""
    service, invoke = _service(
        AnswerReadinessIntent(
            requires_sync_completion=True,
            dependencies=[AnswerDataDependency(target_table="orders")],
            reason="依赖订单汇总。",
        )
    )
    result = await service.evaluate("订单总数", _catalog())
    check_equal("就绪门禁", result.decision, AnswerGateDecision.PROCEED)
    check_equal("就绪无用户提示", result.user_message, None)
    check_equal("就绪工具调用次数", invoke.await_count, 1)


async def test_not_ready_returns_fixed_safe_message() -> None:
    """未就绪时只返回固定稍后重试提示。"""
    service, _ = _service(
        AnswerReadinessIntent(
            requires_sync_completion=True,
            dependencies=[AnswerDataDependency(target_table="orders", source="erp")],
            reason="依赖 ERP 订单。",
        ),
        ready=False,
    )
    result = await service.evaluate("ERP 订单数", _catalog())
    check_equal("未就绪门禁", result.decision, AnswerGateDecision.DATA_PREPARING)
    check_equal("未就绪固定提示", result.user_message, DATA_PREPARING_MESSAGE)
    if result.user_message is None:
        fail_check("未就绪提示存在", actual=None, expected=DATA_PREPARING_MESSAGE)
    check_equal("提示不含目标表", "orders" in result.user_message, False)


async def test_unresolved_intent_fails_closed_without_tool_call() -> None:
    """意图修复失败时拒绝继续且提示重新表述。"""
    service, invoke = _service(None)
    result = await service.evaluate("不明确的问题", _catalog())
    check_equal(
        "意图未解析门禁",
        result.decision,
        AnswerGateDecision.INTENT_UNRESOLVED,
    )
    check_equal("意图未解析提示", result.user_message, INTENT_UNRESOLVED_MESSAGE)
    check_equal("意图未解析不调用工具", invoke.await_count, 0)
