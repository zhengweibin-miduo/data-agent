"""应用日志配置与结构化记录检查。"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from loguru import logger
from pydantic import ValidationError

from data_agent.logging import setup_logging
from data_agent.settings import (
    ConsoleLoggingSettings,
    FileLoggingSettings,
    LoggingSettings,
)
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)


def _logging_config(
    log_dir: Path,
    *,
    output_format: Literal["text", "json"] = "json",
) -> LoggingSettings:
    """创建测试专用的严格日志配置。"""
    return LoggingSettings(
        service_name="test-service",
        deployment_environment="test",
        console=ConsoleLoggingSettings(
            enable=False,
            level="INFO",
            format="text",
        ),
        file=FileLoggingSettings(
            enable=True,
            level="INFO",
            format=output_format,
            path=log_dir,
            rotation="10 MB",
            retention="7 days",
        ),
    )


def _read_json_records(log_dir: Path) -> list[dict[str, object]]:
    """读取每个物理日志行对应的 JSON 对象。"""
    return [
        json.loads(line)
        for line in (log_dir / "data-agent.log")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def test_setup_logging_json() -> None:
    """验证重复配置、默认上下文及扁平 JSON 字段。"""
    with TemporaryDirectory() as directory:
        log_dir = Path(directory) / "logs"
        config = _logging_config(log_dir)

        try:
            setup_logging(config)
            setup_logging(config)
            logger.info("默认日志")
            logger.bind(
                trace_id="trace-1",
                component="test.component",
                event_name="test.operation.completed",
                operation="test_operation",
                outcome="succeeded",
                attempt=2,
                table_count=3,
            ).info("结构化操作完成")

            records = _read_json_records(log_dir)
            check_equal(
                "test_setup_logging_json 检查点 1",
                len(records),
                2,
            )
            check_equal(
                "test_setup_logging_json 检查点 2",
                records[0]["trace_id"],
                "-",
            )
            check_equal(
                "test_setup_logging_json 检查点 3",
                records[0]["event_name"],
                "application.log",
            )
            check_equal(
                "test_setup_logging_json 检查点 4",
                records[1]["attempt"],
                2,
            )
            check_equal(
                "test_setup_logging_json 检查点 5",
                records[1]["table_count"],
                3,
            )
            required = {
                "timestamp",
                "severity",
                "message",
                "event_name",
                "service_name",
                "deployment_environment",
                "component",
                "trace_id",
                "logger_name",
                "function_name",
                "line_number",
                "process_id",
            }
            check_condition(
                "test_setup_logging_json 检查点 6",
                required <= records[1].keys(),
                actual=sorted(records[1]),
                expected=sorted(required),
            )
        finally:
            logger.remove()


def test_setup_logging_text() -> None:
    """验证显式文本格式保留事件、组件和追踪上下文。"""
    with TemporaryDirectory() as directory:
        log_dir = Path(directory) / "logs"
        try:
            setup_logging(_logging_config(log_dir, output_format="text"))
            logger.bind(
                trace_id="trace-text",
                component="test.component",
                event_name="test.text.rendered",
            ).info("中文文本日志")
            output = (log_dir / "data-agent.log").read_text(encoding="utf-8")
            check_condition(
                "test_setup_logging_text 检查点 1",
                "event=test.text.rendered" in output,
                actual=output,
                expected="包含事件名称",
            )
            check_condition(
                "test_setup_logging_text 检查点 2",
                "trace_id=trace-text" in output,
                actual=output,
                expected="包含追踪标识",
            )
            check_condition(
                "test_setup_logging_text 检查点 3",
                "中文文本日志" in output,
                actual=output,
                expected="UTF-8 中文可读",
            )
        finally:
            logger.remove()


async def test_bound_loggers_keep_isolated_trace_context() -> None:
    """验证并发任务中的不可变绑定 logger 不会串写 trace ID。"""
    with TemporaryDirectory() as directory:
        log_dir = Path(directory) / "logs"
        try:
            setup_logging(_logging_config(log_dir))

            async def emit(trace_id: str) -> None:
                """在独立异步任务中写入绑定上下文。"""
                await asyncio.sleep(0)
                logger.bind(
                    trace_id=trace_id,
                    component="test.concurrent",
                    event_name="test.concurrent.emitted",
                ).info("并发日志")

            await asyncio.gather(emit("trace-a"), emit("trace-b"))
            records = _read_json_records(log_dir)
            check_equal(
                "test_bound_loggers_keep_isolated_trace_context 检查点 1",
                {record["trace_id"] for record in records},
                {"trace-a", "trace-b"},
            )
        finally:
            logger.remove()


def test_structured_exception_record() -> None:
    """验证异常记录保持单行且不包含 diagnose 局部变量。"""
    with TemporaryDirectory() as directory:
        log_dir = Path(directory) / "logs"
        try:
            setup_logging(_logging_config(log_dir))
            sensitive_local = "must-not-appear"
            try:
                raise RuntimeError(sensitive_local)
            except RuntimeError as error:
                logger.bind(
                    trace_id="trace-error",
                    component="test.exception",
                    event_name="test.operation.failed",
                    operation="test_operation",
                    outcome="failed",
                    error_code="test_failed",
                    retryable=False,
                ).opt(exception=error).error("测试操作失败")
            records = _read_json_records(log_dir)
            check_equal(
                "test_structured_exception_record 检查点 1",
                len(records),
                1,
            )
            check_equal(
                "test_structured_exception_record 检查点 2",
                records[0]["error_type"],
                "RuntimeError",
            )
            check_condition(
                "test_structured_exception_record 检查点 3",
                "\n" in str(records[0]["stack_trace"]),
                expected="JSON 字符串中保留堆栈换行",
            )
            check_condition(
                "test_structured_exception_record 检查点 4",
                sensitive_local not in json.dumps(records[0]),
                expected="不包含局部变量值或异常消息",
            )
        finally:
            logger.remove()


def test_json_formatter_rejects_non_finite_numbers() -> None:
    """验证非有限浮点值不会生成无效 JSON。"""
    with TemporaryDirectory() as directory:
        log_dir = Path(directory) / "logs"
        try:
            setup_logging(_logging_config(log_dir))
            logger.bind(
                component="test.component",
                event_name="test.operation.completed",
                duration_ms=float("nan"),
            ).info("包含非有限字段的日志")
            records = _read_json_records(log_dir)
            check_equal(
                "test_json_formatter_rejects_non_finite_numbers 检查点 1",
                records[0]["duration_ms"],
                None,
            )
        finally:
            logger.remove()


def test_logging_settings_require_structured_fields() -> None:
    """验证缺少新增字段时严格配置会拒绝启动。"""
    try:
        LoggingSettings.model_validate(
            {
                "console": {"enable": False, "level": "INFO"},
                "file": {
                    "enable": False,
                    "level": "INFO",
                    "path": "logs",
                    "rotation": "10 MB",
                    "retention": "7 days",
                },
            }
        )
    except ValidationError as error:
        check_exception(
            "test_logging_settings_require_structured_fields 捕获预期异常",
            error,
            ValidationError,
        )
    else:
        fail_check(
            "test_logging_settings_require_structured_fields 未捕获异常",
            actual="配置被接受",
            expected="缺少新增字段时抛出 ValidationError",
        )
