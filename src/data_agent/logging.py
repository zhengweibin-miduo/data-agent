"""应用日志配置。"""

from __future__ import annotations

import inspect
import json
import math
import sys
import traceback
from collections.abc import (
    AsyncGenerator,
    Callable,
    Generator,
    Mapping,
)
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
from datetime import UTC
from functools import wraps
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from loguru import logger
from pydantic import BaseModel
from starlette.types import ASGIApp, Receive, Scope, Send

from data_agent.settings import LoggingSettings, app_config

TEXT_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level:<8}</level> | "
    "<cyan>{name}:{function}:{line}</cyan> | "
    "event={extra[event_name]} | component={extra[component]} | "
    "operation={extra[operation]} | trace_id={extra[trace_id]} | "
    "<level>{message}</level>"
)

_APPLICATION_FIELDS = frozenset(
    {
        "request_id",
        "job_id",
        "task_id",
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
_ERROR_LEVEL_NUMBER = 40
_CONTEXT_FIELDS = frozenset(
    {
        "trace_id",
        "component",
        "operation",
        "request_id",
        "job_id",
        "task_id",
        "node_name",
        "attempt",
        "revision",
        "worker_role",
        "error_type",
    }
)
_CONTEXT_ALIASES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "job_try": ("attempt",),
        "lease_token": ("trace_id", "task_id"),
    }
)
_EMPTY_CONTEXT: Mapping[str, object] = MappingProxyType({})
_LOG_CONTEXT: ContextVar[Mapping[str, object]] = ContextVar(
    "data_agent_log_context",
    default=_EMPTY_CONTEXT,
)
ContextFactory = Callable[..., Mapping[str, object]]


def _safe_context(context: Mapping[str, object]) -> dict[str, object]:
    """只保留日志基础设施批准的有界上下文字段。"""
    return {
        key: value
        for key, value in context.items()
        if key in _CONTEXT_FIELDS
        and not isinstance(value, list)
        and _json_field_value(value) is not None
    }


@contextmanager
def logging_context(**context: object) -> Generator[None]:
    """在当前执行上下文中合并日志字段，并在退出时精确恢复。"""
    merged = MappingProxyType({**_LOG_CONTEXT.get(), **_safe_context(context)})
    token = _LOG_CONTEXT.set(merged)
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


def _reflected_context(value: object) -> dict[str, object]:
    """只从受支持容器的白名单字段读取日志上下文。"""
    reflected: dict[str, object] = {}
    try:
        if isinstance(value, Mapping):
            for name in _CONTEXT_FIELDS | _CONTEXT_ALIASES.keys():
                if name in value:
                    for target in _CONTEXT_ALIASES.get(name, (name,)):
                        reflected[target] = value[name]
            return reflected
        if isinstance(value, BaseModel):
            values = vars(value)
            for name in type(value).model_fields:
                if name not in _CONTEXT_FIELDS | _CONTEXT_ALIASES.keys():
                    continue
                if name in values:
                    for target in _CONTEXT_ALIASES.get(name, (name,)):
                        reflected[target] = values[name]
            return reflected
        if is_dataclass(value) and not isinstance(value, type):
            values = vars(value) if hasattr(value, "__dict__") else None
            for field in fields(value):
                if field.name not in _CONTEXT_FIELDS | _CONTEXT_ALIASES.keys():
                    continue
                if values is not None and field.name in values:
                    for target in _CONTEXT_ALIASES.get(field.name, (field.name,)):
                        reflected[target] = values[field.name]
                    continue
                descriptor = inspect.getattr_static(type(value), field.name, None)
                if isinstance(descriptor, property):
                    continue
                field_value = object.__getattribute__(value, field.name)
                for target in _CONTEXT_ALIASES.get(field.name, (field.name,)):
                    reflected[target] = field_value
    except Exception:
        return {}
    return reflected


def _boundary_context(
    component: str,
    operation: str,
    factory: ContextFactory | None,
    signature: inspect.Signature,
    args: tuple[object, ...],
    kwargs: dict[str, object],
) -> dict[str, object]:
    """失败安全地构造一次 AOP 执行边界的日志上下文。"""
    context: dict[str, object] = {
        "component": component,
        "operation": operation,
    }
    try:
        arguments = signature.bind_partial(*args, **kwargs).arguments
    except TypeError:
        arguments = {}
    for name, value in arguments.items():
        if name in _CONTEXT_FIELDS | _CONTEXT_ALIASES.keys():
            for target in _CONTEXT_ALIASES.get(name, (name,)):
                context[target] = value
        for field_name, field_value in _reflected_context(value).items():
            context.setdefault(field_name, field_value)
    if factory is None:
        extracted = {}
    else:
        try:
            extracted = dict(factory(*args, **kwargs))
        except Exception:
            extracted = {}
    context.update(extracted)
    correlation_id = context.get("job_id") or context.get("task_id")
    if isinstance(correlation_id, str):
        context.setdefault("trace_id", correlation_id)
    for value in (*args, *kwargs.values()):
        if isinstance(value, BaseException):
            context.setdefault("error_type", type(value).__name__)
            break
    return context


def logging_boundary(
    *,
    component: str | None = None,
    operation: str | None = None,
    context_factory: ContextFactory | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """为同步、异步或异步生成器 callable 注入隔离的日志上下文。"""

    def decorate(function: Callable[..., Any]) -> Callable[..., Any]:
        """按 callable 类型保留原始调用与传播语义。"""
        boundary_component = component or function.__module__.removeprefix(
            "data_agent."
        )
        boundary_operation = operation or function.__qualname__
        try:
            signature = inspect.signature(function)
        except (TypeError, ValueError):
            signature = inspect.Signature()
        if inspect.isasyncgenfunction(function):

            @wraps(function)
            async def async_generator(
                *args: object,
                **kwargs: object,
            ) -> AsyncGenerator[object, object]:
                context = _boundary_context(
                    boundary_component,
                    boundary_operation,
                    context_factory,
                    signature,
                    args,
                    kwargs,
                )
                iterator = function(*args, **kwargs)
                action = "send"
                action_value: object = None
                iterator_closed = False
                try:
                    while True:
                        try:
                            with logging_context(**context):
                                if action == "throw":
                                    item = await iterator.athrow(action_value)
                                else:
                                    item = await iterator.asend(action_value)
                        except StopAsyncIteration:
                            return
                        try:
                            action_value = yield item
                            action = "send"
                        except GeneratorExit:
                            with logging_context(**context):
                                await iterator.aclose()
                            iterator_closed = True
                            raise
                        except BaseException as error:
                            action = "throw"
                            action_value = error
                finally:
                    if not iterator_closed:
                        with logging_context(**context):
                            await iterator.aclose()

            return async_generator

        if inspect.iscoroutinefunction(function):

            @wraps(function)
            async def asynchronous(
                *args: object,
                **kwargs: object,
            ) -> object:
                context = _boundary_context(
                    boundary_component,
                    boundary_operation,
                    context_factory,
                    signature,
                    args,
                    kwargs,
                )
                with logging_context(**context):
                    return await function(*args, **kwargs)

            return asynchronous

        @wraps(function)
        def synchronous(*args: object, **kwargs: object) -> object:
            context = _boundary_context(
                boundary_component,
                boundary_operation,
                context_factory,
                signature,
                args,
                kwargs,
            )
            with logging_context(**context):
                return function(*args, **kwargs)

        return synchronous

    return decorate


class RequestLoggingContextMiddleware:
    """为一次完整 HTTP 请求及其流式响应建立隔离日志上下文。"""

    def __init__(self, app: ASGIApp) -> None:
        """保存下游 ASGI 应用。"""
        self._app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        """在请求、后台发送和流式迭代结束前保持同一追踪上下文。"""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        request_id = uuid4().hex
        with logging_context(
            trace_id=request_id,
            request_id=request_id,
        ):
            await self._app(scope, receive, send)


def _safe_stack_trace(
    exception_type: type[BaseException],
    exception_traceback: Any,
) -> str:
    """生成不包含异常消息和局部变量的有界堆栈。"""
    stack_trace = "".join(
        (
            "Traceback (most recent call last):\n",
            *traceback.format_tb(exception_traceback, limit=20),
            f"{exception_type.__name__}: <message omitted>\n",
        )
    )
    return stack_trace[:_MAX_STACK_TRACE_CHARACTERS]


def _patch_record(record: Any) -> None:
    """把当前 AOP 与 except 上下文失败安全地注入 Loguru record。"""
    try:
        extra = record["extra"]
        extra.update(_LOG_CONTEXT.get())
        if extra.get("component") in {None, "", "application"}:
            logger_name = record.get("name") or "application"
            extra["component"] = logger_name.removeprefix("data_agent.")
        if extra.get("operation") in {None, "", "-"}:
            extra["operation"] = record.get("function") or "-"
        exception_type, _exception, exception_traceback = sys.exc_info()
        if exception_type is None:
            return
        extra["error_type"] = exception_type.__name__
        if record["level"].no >= _ERROR_LEVEL_NUMBER:
            extra["_safe_stack_trace"] = _safe_stack_trace(
                exception_type,
                exception_traceback,
            )
    except Exception:
        return


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
        "operation": extra.get("operation", "-"),
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

    # 步骤三：优先使用 patcher 生成的安全堆栈；显式 Loguru exception 作为兜底，
    # 两条路径都省略异常消息和局部变量。
    safe_stack_trace = extra.get("_safe_stack_trace")
    if isinstance(safe_stack_trace, str):
        payload["stack_trace"] = safe_stack_trace
    else:
        exception = record["exception"]
        if exception is not None:
            payload.setdefault("error_type", exception.type.__name__)
            payload["stack_trace"] = _safe_stack_trace(
                exception.type,
                exception.traceback,
            )

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
        patcher=_patch_record,
        extra={
            "event_name": "application.log",
            "service_name": config.service_name,
            "deployment_environment": config.deployment_environment,
            "component": "application",
            "operation": "-",
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
