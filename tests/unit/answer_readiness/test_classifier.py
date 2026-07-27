"""回答数据依赖结构化识别检查。"""

from typing import cast
from unittest.mock import AsyncMock, Mock

from langchain_openai import ChatOpenAI

from data_agent.answer_readiness.classifier import AnswerReadinessClassifier
from data_agent.answer_readiness.models import (
    AnswerDataDependency,
    AnswerDataTarget,
    AnswerReadinessIntent,
    AnswerTargetCatalog,
)
from tests.helpers.checks import check_equal


def _catalog() -> AnswerTargetCatalog:
    """返回单目标测试目录。"""
    return AnswerTargetCatalog(
        targets=[AnswerDataTarget(target_table="orders", sources=["erp"])]
    )


def _client_with_results(*results: object) -> tuple[ChatOpenAI, AsyncMock]:
    """返回依次产生指定结构化结果的模型替身。"""
    client = Mock()
    runnable = Mock()
    runnable.ainvoke = AsyncMock(side_effect=list(results))
    client.with_structured_output.return_value = runnable
    return cast(ChatOpenAI, client), runnable.ainvoke


async def test_classifier_returns_first_valid_result() -> None:
    """首次结果合法时不消耗修复调用。"""
    expected = AnswerReadinessIntent(
        requires_sync_completion=False,
        dependencies=[],
        reason="不依赖 DW。",
    )
    client, invoke = _client_with_results(expected)
    actual = await AnswerReadinessClassifier(client).classify("解释主键", _catalog())
    check_equal("首次识别结果", actual, expected)
    check_equal("首次合法调用次数", invoke.await_count, 1)


async def test_classifier_repairs_once_after_invalid_result() -> None:
    """首次结构或目录无效时只执行一次修复。"""
    invalid = AnswerReadinessIntent(
        requires_sync_completion=True,
        dependencies=[AnswerDataDependency(target_table="hallucinated")],
        reason="无效目录。",
    )
    repaired = AnswerReadinessIntent(
        requires_sync_completion=True,
        dependencies=[AnswerDataDependency(target_table="orders", source="erp")],
        reason="查询 ERP 订单。",
    )
    client, invoke = _client_with_results(invalid, repaired)
    actual = await AnswerReadinessClassifier(client).classify("ERP 订单数", _catalog())
    check_equal("修复后的识别结果", actual, repaired)
    check_equal("只允许一次修复", invoke.await_count, 2)
    second_payload = invoke.await_args_list[1].args[0][1][1]
    check_equal(
        "修复提示不回传原始模型结果",
        "hallucinated" in second_payload,
        False,
    )


async def test_classifier_fails_closed_after_second_invalid_result() -> None:
    """第二次结果仍无效时返回未解析，不进入回答路径。"""
    client, invoke = _client_with_results("bad", {"still": "bad"})
    actual = await AnswerReadinessClassifier(client).classify("订单数", _catalog())
    check_equal("持续无效结果", actual, None)
    check_equal("修复预算封顶", invoke.await_count, 2)


async def test_classifier_rejects_unbounded_question_without_model_call() -> None:
    """空问题和超长问题不发送到模型。"""
    client, invoke = _client_with_results()
    classifier = AnswerReadinessClassifier(client)
    check_equal("空问题拒绝", await classifier.classify("", _catalog()), None)
    check_equal(
        "超长问题拒绝",
        await classifier.classify("x" * 8001, _catalog()),
        None,
    )
    check_equal("无模型调用", invoke.await_count, 0)
