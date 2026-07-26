"""应用日志配置。"""

import json
import math
import sys
import traceback
from datetime import UTC
from typing import Any

from loguru import logger

from data_agent.settings import LoggingSettings, app_config

TEXT_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level:<8}</level> | "
    "<cyan>{name}:{function}:{line}</cyan> | "
    "event={extra[event_name]} | component={extra[component]} | "
    "trace_id={extra[trace_id]} | <level>{message}</level>"
)

_APPLICATION_FIELDS = frozenset(
    {
        "operation",
        "outcome",
        "node_name",
        "job_status",
        "attempt",
        "revision",
        "question_round",
        "duration_ms",
        "table_count",
        "column_count",
        "metric_count",
        "question_count",
        "rebuild_count",
        "succeeded_count",
        "failed_count",
        "error_code",
        "error_type",
        "stage",
        "retryable",
        "worker_role",
    }
)
_MAX_STACK_TRACE_CHARACTERS = 16_384


def _json_field_value(value: object) -> object | None:
    """把允许的应用字段收敛为严格 JSON 标量或标量列表。"""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, list):
        normalized = [_json_field_value(item) for item in value]
        if all(
            isinstance(item, str | int | float | bool) or item is None
            for item in normalized
        ):
            return normalized
    return None


def _json_formatter(record: Any) -> str:
    """把 Loguru 记录投影为单行扁平 JSON。"""
    # 步骤一：先构造固定公共字段，确保每条记录都具备可检索的基础上下文。
    extra = record["extra"]
    payload: dict[str, object] = {
        "timestamp": record["time"]
        .astimezone(UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        "severity": record["level"].name,
        "message": record["message"],
        "event_name": extra.get("event_name", "application.log"),
        "service_name": extra.get("service_name", "data-agent"),
        "deployment_environment": extra.get(
            "deployment_environment",
            "unknown",
        ),
        "component": extra.get("component", "application"),
        "trace_id": extra.get("trace_id", "-"),
        "logger_name": record["name"],
        "function_name": record["function"],
        "line_number": record["line"],
        "process_id": record["process"].id,
    }
    # 步骤二：只接纳白名单应用字段，并把值收敛为严格 JSON 可编码类型。
    for key in _APPLICATION_FIELDS:
        if key in extra:
            payload[key] = _json_field_value(extra[key])

    # 步骤三：异常记录补充错误类型和有界堆栈，并在堆栈尾部省略异常消息。
    exception = record["exception"]
    if exception is not None:
        payload.setdefault("error_type", exception.type.__name__)
        stack_trace = "".join(
            (
                "Traceback (most recent call last):\n",
                *traceback.format_tb(exception.traceback, limit=20),
                f"{exception.type.__name__}: <message omitted>\n",
            )
        )
        payload["stack_trace"] = stack_trace[:_MAX_STACK_TRACE_CHARACTERS]

    # 步骤四：把扁平载荷序列化到 Loguru extra，由统一模板输出为单行 JSON。
    extra["_serialized_record"] = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return "{extra[_serialized_record]}\n"


def _sink_format(output_format: str) -> str | Any:
    """按显式配置选择文本或 JSON formatter。"""
    return TEXT_FORMAT if output_format == "text" else _json_formatter


def setup_logging(config: LoggingSettings = app_config.logging) -> None:
    """根据应用配置重建 Loguru sinks。"""
    # 步骤一：移除既有 sinks，并为所有后续记录设置稳定的共享上下文字段。
    logger.remove()
    logger.configure(
        extra={
            "event_name": "application.log",
            "service_name": config.service_name,
            "deployment_environment": config.deployment_environment,
            "component": "application",
            "trace_id": "-",
        }
    )

    # 步骤二：按控制台开关和格式装配 stderr sink。
    if config.console.enable:
        logger.add(
            sys.stderr,
            level=config.console.level,
            format=_sink_format(config.console.format),
            colorize=config.console.format == "text",
            diagnose=False,
            enqueue=True,
        )

    # 步骤三：按文件开关先准备目录，再装配具备轮转与保留策略的文件 sink。
    if config.file.enable:
        config.file.path.mkdir(parents=True, exist_ok=True)
        logger.add(
            config.file.path / "data-agent.log",
            level=config.file.level,
            format=_sink_format(config.file.format),
            colorize=False,
            rotation=config.file.rotation,
            retention=config.file.retention,
            encoding="utf-8",
            diagnose=False,
            enqueue=True,
        )
