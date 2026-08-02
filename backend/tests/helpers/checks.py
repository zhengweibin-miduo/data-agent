"""提供可观察且保留自动失败语义的测试检查函数。"""

import os
from typing import NoReturn

import pytest

_USE_CONDITION = object()
_LAST_TEST_ID: str | None = None


def check_equal(label: str, actual: object, expected: object) -> None:
    """输出相等性检查结果，并在不匹配时使测试失败。

    Args:
        label: 用于识别测试场景的标签。
        actual: 被测代码产生的实际值。
        expected: 测试期望值。
    """
    passed = actual == expected
    _report(label, passed=passed, actual=actual, expected=expected)


def check_condition(
    label: str,
    condition: object,
    *,
    actual: object = _USE_CONDITION,
    expected: object = "条件为真",
) -> None:
    """输出条件检查结果，并在条件不成立时使测试失败。

    Args:
        label: 用于识别测试场景的标签。
        condition: 按 Python 真值规则判断的检查结果。
        actual: 需要展示的实际值；省略时展示条件结果。
        expected: 人类可读的期望值或期望描述。
    """
    displayed_actual = condition if actual is _USE_CONDITION else actual
    _report(
        label,
        passed=bool(condition),
        actual=displayed_actual,
        expected=expected,
    )


def check_exception(
    label: str,
    error: BaseException,
    expected_type: type[BaseException] | tuple[type[BaseException], ...],
) -> None:
    """输出捕获到的异常类型和消息，并检查异常类型。

    Args:
        label: 用于识别预期异常场景的标签。
        error: 被测代码实际抛出的异常。
        expected_type: 允许的异常类型。
    """
    expected_names = (
        tuple(item.__name__ for item in expected_type)
        if isinstance(expected_type, tuple)
        else expected_type.__name__
    )
    _report(
        label,
        passed=isinstance(error, expected_type),
        actual={"type": type(error).__name__, "message": str(error)},
        expected={"type": expected_names},
    )


def fail_check(label: str, *, actual: object, expected: object) -> NoReturn:
    """输出显式失败结果，并使测试立即失败。

    Args:
        label: 用于识别测试场景的标签。
        actual: 导致失败的实际结果。
        expected: 人类可读的期望值或期望描述。
    """
    message = f"[FAIL] {label} | actual={actual!r} | expected={expected!r}"
    _print_result(message)
    pytest.fail(message, pytrace=False)


def _report(
    label: str,
    *,
    passed: bool,
    actual: object,
    expected: object,
) -> None:
    """输出统一检查结果，并在失败时交由 pytest 终止测试。"""
    status = "PASS" if passed else "FAIL"
    message = f"[{status}] {label} | actual={actual!r} | expected={expected!r}"
    _print_result(message)
    if not passed:
        pytest.fail(message, pytrace=False)


def _print_result(message: str) -> None:
    """输出检查结果，并在当前测试的首条结果前换行。"""
    global _LAST_TEST_ID

    current_test_id = os.environ.get("PYTEST_CURRENT_TEST")
    if current_test_id is not None and current_test_id != _LAST_TEST_ID:
        print()
        _LAST_TEST_ID = current_test_id
    print(message)
