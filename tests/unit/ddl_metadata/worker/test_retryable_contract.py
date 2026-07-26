"""worker 可重试错误判定的契约测试。"""

from __future__ import annotations

from openai import APITimeoutError
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.exc import OperationalError

from data_agent.ddl_metadata.worker.job_runner import _is_retryable
from data_agent.errors import DataAgentError
from tests.helpers.checks import check_equal


def _error(*, retryable: bool) -> DataAgentError:
    """构造指定可重试声明的业务错误。"""
    return DataAgentError(
        "llm_unavailable",
        "generate_metadata",
        "模型服务暂时不可用",
        retryable=retryable,
    )


def test_data_agent_error_declaration_is_authoritative() -> None:
    """DataAgentError 的 retryable 声明必须被读取，而不是被异常类型匹配覆盖。"""
    check_equal("显式声明可重试", _is_retryable(_error(retryable=True)), True)
    check_equal("显式声明不可重试", _is_retryable(_error(retryable=False)), False)


def test_non_retryable_declaration_wins_over_builtin_list() -> None:
    """即使异常同时是内置瞬态类型，显式声明不可重试也必须被尊重。"""

    class DeclaredPermanent(DataAgentError, TimeoutError):
        """同时属于内置瞬态清单与业务错误契约的异常。"""

    error = DeclaredPermanent(
        "ddl_invalid",
        "parse_ddl",
        "DDL 无法解析",
        retryable=False,
    )
    check_equal("声明优先于类型匹配", _is_retryable(error), False)
    check_equal(
        "该异常确实属于内置瞬态类型",
        isinstance(error, TimeoutError),
        True,
    )


def test_third_party_transient_errors_fall_back_to_builtin_list() -> None:
    """无法自我描述的第三方瞬态异常仍按内置清单判定为可重试。"""
    cases = (
        ("模型请求超时", APITimeoutError(request=None)),  # type: ignore[arg-type]
        ("Redis 连接中断", RedisConnectionError("connection reset")),
        (
            "数据库连接失效",
            OperationalError("SELECT 1", {}, Exception("gone away")),
        ),
        ("标准库超时", TimeoutError("timed out")),
        ("标准库连接错误", ConnectionError("refused")),
    )
    for label, error in cases:
        check_equal(label, _is_retryable(error), True)


def test_unknown_third_party_errors_are_not_retried() -> None:
    """未登记的第三方异常按不可重试处理，避免无依据地消耗重试预算。"""
    check_equal("未登记异常", _is_retryable(ValueError("bad payload")), False)
