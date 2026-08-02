"""回答数据就绪门禁契约检查。"""

import pytest
from pydantic import ValidationError

from answer_readiness.models import (
    AnswerDataDependency,
    AnswerDataTarget,
    AnswerReadinessIntent,
    AnswerTargetCatalog,
)
from tests.helpers.checks import check_condition, check_equal, check_exception


def _catalog() -> AnswerTargetCatalog:
    """返回测试使用的有界目标目录。"""
    return AnswerTargetCatalog(
        targets=[
            AnswerDataTarget(
                target_table="orders",
                sources=["erp", "store"],
            ),
            AnswerDataTarget(target_table="customers", sources=["crm"]),
        ]
    )


def test_valid_no_wait_source_scoped_and_aggregate_intents() -> None:
    """接受无等待、来源限定和全来源汇总三种合法意图。"""
    catalog = _catalog()
    intents = [
        AnswerReadinessIntent(
            requires_sync_completion=False,
            dependencies=[],
            reason="问题不依赖 DW 数据。",
        ),
        AnswerReadinessIntent(
            requires_sync_completion=True,
            dependencies=[AnswerDataDependency(target_table="orders", source="erp")],
            reason="问题明确查询 ERP 订单。",
        ),
        AnswerReadinessIntent(
            requires_sync_completion=True,
            dependencies=[AnswerDataDependency(target_table="orders")],
            reason="问题汇总全部订单来源。",
        ),
    ]
    for intent in intents:
        intent.validate_catalog(catalog)
    check_equal("合法意图数量", len(intents), 3)


@pytest.mark.parametrize(
    ("requires_sync_completion", "dependencies"),
    [
        (True, []),
        (False, [AnswerDataDependency(target_table="orders")]),
        (
            True,
            [
                AnswerDataDependency(target_table="orders"),
                AnswerDataDependency(target_table="orders", source="erp"),
            ],
        ),
    ],
)
def test_invalid_consistency_or_duplicate_is_rejected(
    requires_sync_completion: bool,
    dependencies: list[AnswerDataDependency],
) -> None:
    """拒绝缺失依赖、意外依赖和重复目标。"""
    with pytest.raises(ValidationError) as captured:
        AnswerReadinessIntent(
            requires_sync_completion=requires_sync_completion,
            dependencies=dependencies,
            reason="无效意图。",
        )
    check_exception("无效意图校验失败", captured.value, ValidationError)


def test_hallucinated_target_or_source_is_rejected() -> None:
    """确定性目录校验拒绝模型臆造的目标和来源。"""
    catalog = _catalog()
    for dependency in (
        AnswerDataDependency(target_table="missing"),
        AnswerDataDependency(target_table="orders", source="missing"),
    ):
        intent = AnswerReadinessIntent(
            requires_sync_completion=True,
            dependencies=[dependency],
            reason="臆造依赖。",
        )
        with pytest.raises(ValueError) as captured:
            intent.validate_catalog(catalog)
        check_exception("目录外依赖校验失败", captured.value, ValueError)


def test_oversized_dependency_result_is_rejected() -> None:
    """拒绝超过工具和路由上限的依赖列表。"""
    with pytest.raises(ValidationError) as captured:
        AnswerReadinessIntent(
            requires_sync_completion=True,
            dependencies=[
                AnswerDataDependency(target_table=f"target_{index}")
                for index in range(21)
            ],
            reason="超出上限。",
        )
    check_condition(
        "超长依赖命中列表上限",
        "dependencies" in str(captured.value),
        actual=str(captured.value),
        expected="包含 dependencies 校验错误",
    )
