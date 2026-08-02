"""API 与 worker 日志排空生命周期检查。"""

from typing import Any
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from pytest import MonkeyPatch

import application
from ddl_metadata.worker import lifecycle
from tests.helpers.checks import check_equal, check_exception, fail_check


class _RecordingLogger:
    """记录生命周期日志与 complete 调用顺序。"""

    def __init__(self, events: list[str]) -> None:
        """绑定顺序列表。"""
        self._events = events

    def info(self, message: str) -> None:
        """把业务消息投影为测试使用的稳定生命周期标记。"""
        event_by_message = {
            "API 服务已启动，数据库、缓存与派生检索资源均已就绪": (
                "application.lifecycle.started"
            ),
            "API 服务已停止，进程内共享资源已经关闭": (
                "application.lifecycle.stopped"
            ),
            "DDL 元数据 worker 已停止，进程内共享资源已经关闭": (
                "application.lifecycle.stopped"
            ),
        }
        self._events.append(event_by_message.get(message, message))

    async def complete(self) -> None:
        """记录队列排空。"""
        self._events.append("complete")


def _close_spy(
    events: list[str],
    name: str,
    *,
    error: Exception | None = None,
) -> AsyncMock:
    """构造记录顺序并可注入失败的异步关闭函数。"""

    async def close() -> None:
        """记录关闭并传播指定异常。"""
        events.append(name)
        if error is not None:
            raise error

    return AsyncMock(side_effect=close)


def _patch_api_startup(monkeypatch: MonkeyPatch) -> None:
    """把 API 初始化替换为无外部 I/O 的构造。"""
    monkeypatch.setattr(application, "setup_logging", Mock())
    for manager in (
        application.RedisClient,
        application.MySQLDatabase,
        application.ElasticsearchClient,
        application.QdrantClient,
        application.TEIEmbeddingClient,
        application.LLMClient,
    ):
        monkeypatch.setattr(manager, "initialize", Mock(return_value=object()))


def _patch_closes(
    monkeypatch: MonkeyPatch,
    events: list[str],
    targets: tuple[tuple[type[Any], str], ...],
    *,
    failure_name: str | None = None,
    failure: Exception | None = None,
) -> None:
    """按名称给资源管理器安装关闭顺序 spy。"""
    for manager, name in targets:
        monkeypatch.setattr(
            manager,
            "close",
            _close_spy(
                events,
                name,
                error=failure if name == failure_name else None,
            ),
        )


_API_CLOSES = (
    (application.LLMClient, "llm"),
    (application.TEIEmbeddingClient, "tei"),
    (application.QdrantClient, "qdrant"),
    (application.ElasticsearchClient, "elasticsearch"),
    (application.MySQLDatabase, "mysql"),
    (application.RedisClient, "redis"),
)
_WORKER_CLOSES = (
    (lifecycle.CheckpointStore, "checkpoint"),
    (lifecycle.LLMClient, "llm"),
    (lifecycle.TEIEmbeddingClient, "tei"),
    (lifecycle.QdrantClient, "qdrant"),
    (lifecycle.ElasticsearchClient, "elasticsearch"),
    (lifecycle.MySQLDatabase, "mysql"),
    (lifecycle.RedisClient, "redis"),
)


async def test_api_lifespan_drains_after_final_stopped_log(
    monkeypatch: MonkeyPatch,
) -> None:
    """API 正常关闭在逆序资源关闭和 stopped 事件后排空日志。"""
    events: list[str] = []
    _patch_api_startup(monkeypatch)
    _patch_closes(monkeypatch, events, _API_CLOSES)
    monkeypatch.setattr(application, "logger", _RecordingLogger(events))

    async with application._lifespan(FastAPI()):
        events.append("body")

    check_equal(
        "API 正常关闭顺序",
        events,
        [
            "application.lifecycle.started",
            "body",
            "llm",
            "tei",
            "qdrant",
            "elasticsearch",
            "mysql",
            "redis",
            "application.lifecycle.stopped",
            "complete",
        ],
    )


async def test_api_lifespan_drains_when_close_fails(
    monkeypatch: MonkeyPatch,
) -> None:
    """API 资源关闭失败仍排空日志并传播原异常。"""
    events: list[str] = []
    expected = RuntimeError("api close failed")
    _patch_api_startup(monkeypatch)
    _patch_closes(
        monkeypatch,
        events,
        _API_CLOSES,
        failure_name="tei",
        failure=expected,
    )
    monkeypatch.setattr(application, "logger", _RecordingLogger(events))

    try:
        async with application._lifespan(FastAPI()):
            events.append("body")
    except RuntimeError as error:
        check_exception("API 关闭异常类型", error, RuntimeError)
        check_equal("API 传播原关闭异常", error is expected, True)
    else:
        fail_check(
            "API 关闭失败",
            actual="未抛出异常",
            expected="传播原 RuntimeError",
        )
    check_equal(
        "API 关闭失败仍排空",
        events,
        ["application.lifecycle.started", "body", "llm", "tei", "complete"],
    )


async def test_worker_shutdown_drains_after_final_stopped_log(
    monkeypatch: MonkeyPatch,
) -> None:
    """Worker 正常关闭在全部资源和 stopped 事件后排空日志。"""
    events: list[str] = []
    _patch_closes(monkeypatch, events, _WORKER_CLOSES)
    monkeypatch.setattr(lifecycle, "logger", _RecordingLogger(events))

    await lifecycle.shutdown({})

    check_equal(
        "worker 正常关闭顺序",
        events,
        [
            "checkpoint",
            "llm",
            "tei",
            "qdrant",
            "elasticsearch",
            "mysql",
            "redis",
            "application.lifecycle.stopped",
            "complete",
        ],
    )


async def test_worker_shutdown_drains_when_close_fails(
    monkeypatch: MonkeyPatch,
) -> None:
    """Worker 资源关闭失败仍排空日志并传播原异常。"""
    events: list[str] = []
    expected = RuntimeError("worker close failed")
    _patch_closes(
        monkeypatch,
        events,
        _WORKER_CLOSES,
        failure_name="checkpoint",
        failure=expected,
    )
    monkeypatch.setattr(lifecycle, "logger", _RecordingLogger(events))

    try:
        await lifecycle.shutdown({})
    except RuntimeError as error:
        check_exception("worker 关闭异常类型", error, RuntimeError)
        check_equal("worker 传播原关闭异常", error is expected, True)
    else:
        fail_check(
            "worker 关闭失败",
            actual="未抛出异常",
            expected="传播原 RuntimeError",
        )
    check_equal(
        "worker 关闭失败仍排空",
        events,
        ["checkpoint", "complete"],
    )
