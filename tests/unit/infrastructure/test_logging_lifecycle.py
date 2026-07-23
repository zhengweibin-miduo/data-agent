"""统一运行时装配与日志排空生命周期检查。"""

from asyncio import CancelledError
from typing import Any, cast
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from pytest import MonkeyPatch

from data_agent import application, runtime
from data_agent.ddl_metadata.worker import lifecycle
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)


class _BoundRecordingLogger:
    """记录一次绑定日志事件。"""

    def __init__(self, events: list[str], event_name: str) -> None:
        """绑定记录目标与事件名。"""
        self._events = events
        self._event_name = event_name

    def info(self, message: str) -> None:
        """记录 INFO 事件。"""
        del message
        self._events.append(self._event_name)

    def warning(self, message: str) -> None:
        """记录 WARNING 事件。"""
        del message
        self._events.append(self._event_name)


class _RecordingLogger:
    """记录生命周期日志与 complete 调用顺序。"""

    def __init__(self, events: list[str]) -> None:
        """绑定顺序列表。"""
        self._events = events

    def bind(self, **fields: object) -> _BoundRecordingLogger:
        """返回携带事件名的记录器。"""
        return _BoundRecordingLogger(
            self._events,
            str(fields.get("event_name", "application.log")),
        )

    async def complete(self) -> None:
        """记录队列排空。"""
        self._events.append("complete")


def _sync_spy(
    events: list[str],
    name: str,
    *,
    value: object | None = None,
    error: BaseException | None = None,
) -> Mock:
    """构造记录顺序并可注入失败的同步函数。"""

    def call() -> object | None:
        """记录调用并返回或传播指定结果。"""
        events.append(name)
        if error is not None:
            raise error
        return value

    return Mock(side_effect=call)


def _async_spy(
    events: list[str],
    name: str,
    *,
    error: BaseException | None = None,
) -> AsyncMock:
    """构造记录顺序并可注入失败的异步函数。"""

    async def call(*args: object, **kwargs: object) -> None:
        """记录调用并传播指定异常。"""
        del args, kwargs
        events.append(name)
        if error is not None:
            raise error

    return AsyncMock(side_effect=call)


def _patch_api_plan(
    monkeypatch: MonkeyPatch,
    events: list[str],
    *,
    initializer_errors: dict[str, BaseException] | None = None,
    close_errors: dict[str, BaseException] | None = None,
) -> None:
    """把 API 计划替换为可观察且无外部 I/O 的实现。"""
    initializer_errors = initializer_errors or {}
    close_errors = close_errors or {}
    monkeypatch.setattr(runtime, "logger", _RecordingLogger(events))
    monkeypatch.setattr(
        runtime,
        "setup_logging",
        _sync_spy(events, "logging", error=initializer_errors.get("logging")),
    )
    managers = (
        (runtime.RedisClient, "redis"),
        (runtime.MySQLDatabase, "mysql"),
        (runtime.ElasticsearchClient, "elasticsearch"),
        (runtime.QdrantClient, "qdrant"),
        (runtime.TEIEmbeddingClient, "tei"),
    )
    for manager, name in managers:
        monkeypatch.setattr(
            manager,
            "initialize",
            _sync_spy(
                events,
                f"start:{name}",
                value=object(),
                error=initializer_errors.get(name),
            ),
        )
        monkeypatch.setattr(
            manager,
            "close",
            _async_spy(
                events,
                f"close:{name}",
                error=close_errors.get(name),
            ),
        )
    jobs = object()
    monkeypatch.setattr(runtime, "DDLJobStore", Mock(return_value=jobs))
    monkeypatch.setattr(runtime, "MemoryService", Mock(return_value=object()))
    monkeypatch.setattr(runtime, "ConversationService", Mock(return_value=object()))


async def test_api_runtime_publishes_state_and_closes_in_reverse_order(
    monkeypatch: MonkeyPatch,
) -> None:
    """API 通过公开接口发布原状态键并逆序关闭资源。"""
    events: list[str] = []
    _patch_api_plan(monkeypatch, events)
    target = FastAPI().state

    handle = await runtime.start(runtime.RuntimeRole.API, target)
    check_condition("API 发布 jobs", hasattr(target, "jobs"))
    check_condition("API 发布 memories", hasattr(target, "memories"))
    check_condition("API 发布 conversations", hasattr(target, "conversations"))
    events.append("body")
    await runtime.stop(handle)

    check_equal(
        "API 完整生命周期顺序",
        events,
        [
            "logging",
            "start:redis",
            "start:mysql",
            "start:elasticsearch",
            "start:qdrant",
            "start:tei",
            "application.lifecycle.started",
            "body",
            "close:tei",
            "close:qdrant",
            "close:elasticsearch",
            "close:mysql",
            "close:redis",
            "application.lifecycle.stopped",
            "complete",
        ],
    )
    check_equal("API 关闭后移除 jobs", hasattr(target, "jobs"), False)
    check_equal("API 关闭后移除 memories", hasattr(target, "memories"), False)
    check_equal("API 关闭后移除 conversations", hasattr(target, "conversations"), False)


async def test_worker_runtime_preserves_state_and_role_specific_order(
    monkeypatch: MonkeyPatch,
) -> None:
    """Worker 计划保留索引、模型、图、维护和公开状态顺序。"""
    events: list[str] = []
    _patch_api_plan(monkeypatch, events)
    model = object()
    checkpointer = object()
    graph = object()
    extractor = object()
    monkeypatch.setattr(
        runtime.MemoryElasticsearchIndex,
        "setup",
        _async_spy(events, "setup:elasticsearch"),
    )
    monkeypatch.setattr(
        runtime.MemoryQdrantIndex,
        "setup",
        _async_spy(events, "setup:qdrant"),
    )
    monkeypatch.setattr(
        runtime.LLMClient,
        "initialize",
        _sync_spy(events, "start:llm", value=model),
    )
    monkeypatch.setattr(
        runtime.LLMClient,
        "check_structured_output_capability",
        _async_spy(events, "check:llm"),
    )
    monkeypatch.setattr(
        runtime.LLMClient,
        "close",
        _async_spy(events, "close:llm"),
    )
    monkeypatch.setattr(
        runtime.CheckpointStore,
        "initialize",
        AsyncMock(
            side_effect=lambda: (
                events.append("start:checkpoint"),
                checkpointer,
            )[1]
        ),
    )
    monkeypatch.setattr(
        runtime.CheckpointStore,
        "close",
        _async_spy(events, "close:checkpoint"),
    )
    monkeypatch.setattr(
        runtime,
        "ConversationMemoryExtractor",
        Mock(return_value=extractor),
    )
    monkeypatch.setattr(runtime, "LLMMetadataGenerator", Mock(return_value=object()))
    monkeypatch.setattr(runtime, "MemoryContextLoader", Mock(return_value=object()))
    monkeypatch.setattr(
        runtime,
        "MetadataSnapshotService",
        Mock(return_value=object()),
    )
    monkeypatch.setattr(runtime, "build_ddl_metadata_graph", Mock(return_value=graph))
    monkeypatch.setattr(
        runtime,
        "dispatch_pending",
        _async_spy(events, "maintenance:dispatch"),
    )
    monkeypatch.setattr(
        runtime,
        "cleanup_checkpoints",
        _async_spy(events, "maintenance:cleanup"),
    )
    ctx: dict[str, Any] = {}

    handle = await runtime.start(runtime.RuntimeRole.DDL_METADATA_WORKER, ctx)
    check_condition("Worker 发布 jobs", "jobs" in ctx)
    check_equal("Worker 发布 extractor", ctx["conversation_extractor"], extractor)
    check_equal("Worker 发布 graph", ctx["graph"], graph)
    await runtime.stop(handle)

    check_equal(
        "Worker 角色专属顺序",
        events[6:14],
        [
            "setup:elasticsearch",
            "setup:qdrant",
            "start:llm",
            "check:llm",
            "start:checkpoint",
            "maintenance:dispatch",
            "maintenance:cleanup",
            "application.lifecycle.started",
        ],
    )
    check_equal(
        "Worker 逆序关闭顺序",
        events[-9:],
        [
            "close:checkpoint",
            "close:llm",
            "close:tei",
            "close:qdrant",
            "close:elasticsearch",
            "close:mysql",
            "close:redis",
            "application.lifecycle.stopped",
            "complete",
        ],
    )


async def test_startup_failure_rolls_back_only_completed_actions(
    monkeypatch: MonkeyPatch,
) -> None:
    """启动失败只逆序回滚已经完成的 action 并传播原异常。"""
    events: list[str] = []
    expected = RuntimeError("qdrant start failed")
    _patch_api_plan(
        monkeypatch,
        events,
        initializer_errors={"qdrant": expected},
    )

    try:
        await runtime.start(runtime.RuntimeRole.API, FastAPI().state)
    except RuntimeError as error:
        check_equal("启动异常保持对象身份", error is expected, True)
    else:
        fail_check("API 启动失败", actual="未抛出异常", expected="原 RuntimeError")
    check_equal(
        "启动失败逆序回滚",
        events,
        [
            "logging",
            "start:redis",
            "start:mysql",
            "start:elasticsearch",
            "start:qdrant",
            "close:elasticsearch",
            "close:mysql",
            "close:redis",
            "complete",
        ],
    )


async def test_rollback_failure_does_not_replace_startup_error(
    monkeypatch: MonkeyPatch,
) -> None:
    """回滚关闭失败被安全记录且不替换原启动异常。"""
    events: list[str] = []
    expected = RuntimeError("qdrant start failed")
    _patch_api_plan(
        monkeypatch,
        events,
        initializer_errors={"qdrant": expected},
        close_errors={"elasticsearch": RuntimeError("close failed")},
    )

    try:
        await runtime.start(runtime.RuntimeRole.API, FastAPI().state)
    except RuntimeError as error:
        check_equal("回滚失败仍传播启动异常", error is expected, True)
    else:
        fail_check("回滚失败", actual="未抛出异常", expected="原启动异常")
    check_equal(
        "回滚失败仍继续并排空日志",
        events[-5:],
        [
            "close:elasticsearch",
            "application.lifecycle.rollback_failed",
            "close:mysql",
            "close:redis",
            "complete",
        ],
    )


async def test_rollback_log_failure_does_not_replace_startup_error(
    monkeypatch: MonkeyPatch,
) -> None:
    """回滚诊断日志失败也不替换启动异常或中断剩余清理。"""
    events: list[str] = []
    expected = RuntimeError("qdrant start failed")
    _patch_api_plan(
        monkeypatch,
        events,
        initializer_errors={"qdrant": expected},
        close_errors={"elasticsearch": RuntimeError("close failed")},
    )
    complete = AsyncMock(side_effect=lambda: events.append("complete"))
    bound = Mock()
    bound.warning.side_effect = RuntimeError("logging failed")
    failing_logger = Mock()
    failing_logger.bind.return_value = bound
    failing_logger.complete = complete
    monkeypatch.setattr(runtime, "logger", failing_logger)

    try:
        await runtime.start(runtime.RuntimeRole.API, FastAPI().state)
    except RuntimeError as error:
        check_equal("日志失败仍传播启动异常", error is expected, True)
    else:
        fail_check("回滚日志失败", actual="未抛出异常", expected="原启动异常")
    check_equal(
        "日志失败仍继续剩余清理",
        events[-4:],
        ["close:elasticsearch", "close:mysql", "close:redis", "complete"],
    )


async def test_stop_continues_after_single_failure_and_preserves_identity(
    monkeypatch: MonkeyPatch,
) -> None:
    """单个关闭失败不阻止后续关闭并保持异常对象身份。"""
    events: list[str] = []
    expected = RuntimeError("tei close failed")
    _patch_api_plan(monkeypatch, events, close_errors={"tei": expected})
    handle = await runtime.start(runtime.RuntimeRole.API, FastAPI().state)

    try:
        await runtime.stop(handle)
    except RuntimeError as error:
        check_equal("单关闭异常保持对象身份", error is expected, True)
    else:
        fail_check("API 关闭失败", actual="未抛出异常", expected="原 RuntimeError")
    check_equal(
        "单关闭失败仍完成全部动作",
        events[-7:],
        [
            "close:tei",
            "close:qdrant",
            "close:elasticsearch",
            "close:mysql",
            "close:redis",
            "application.lifecycle.stopped",
            "complete",
        ],
    )


async def test_stop_groups_multiple_failures(
    monkeypatch: MonkeyPatch,
) -> None:
    """多个关闭失败按实际关闭顺序汇总为 ExceptionGroup。"""
    events: list[str] = []
    first = RuntimeError("tei close failed")
    second = ValueError("mysql close failed")
    _patch_api_plan(
        monkeypatch,
        events,
        close_errors={"tei": first, "mysql": second},
    )
    handle = await runtime.start(runtime.RuntimeRole.API, FastAPI().state)

    try:
        await runtime.stop(handle)
    except ExceptionGroup as error:
        check_equal("多关闭异常对象顺序", error.exceptions, (first, second))
    else:
        fail_check("多关闭异常", actual="未抛出异常", expected="ExceptionGroup")
    check_equal("多关闭失败后仍排空日志", events[-1], "complete")


async def test_startup_failure_restores_every_published_state_key(
    monkeypatch: MonkeyPatch,
) -> None:
    """后段启动失败恢复旧状态并移除本次新增状态，避免悬空资源引用。"""
    events: list[str] = []
    _patch_api_plan(monkeypatch, events)
    expected = RuntimeError("conversation construction failed")
    old_jobs = object()
    target = FastAPI().state
    target.jobs = old_jobs
    monkeypatch.setattr(runtime, "ConversationService", Mock(side_effect=expected))

    try:
        await runtime.start(runtime.RuntimeRole.API, target)
    except RuntimeError as error:
        check_equal("状态发布失败保持异常身份", error is expected, True)
    else:
        fail_check("API 状态发布失败", actual="未抛出异常", expected="原 RuntimeError")

    check_equal("恢复启动前 jobs", target.jobs is old_jobs, True)
    check_equal("移除部分发布 memories", hasattr(target, "memories"), False)
    check_equal("未残留 conversations", hasattr(target, "conversations"), False)
    check_equal("状态恢复后仍排空日志", events[-1], "complete")


async def test_stop_continues_after_cancelled_error_and_drains_logging(
    monkeypatch: MonkeyPatch,
) -> None:
    """资源关闭取消仍继续清理并排空日志，单异常保持对象身份。"""
    events: list[str] = []
    expected = CancelledError("tei close cancelled")
    _patch_api_plan(monkeypatch, events, close_errors={"tei": expected})
    handle = await runtime.start(runtime.RuntimeRole.API, FastAPI().state)

    try:
        await runtime.stop(handle)
    except CancelledError as error:
        check_equal("取消异常保持对象身份", error is expected, True)
    else:
        fail_check("API 关闭取消", actual="未抛出异常", expected="原 CancelledError")
    check_equal(
        "取消后继续关闭并排空日志",
        events[-7:],
        [
            "close:tei",
            "close:qdrant",
            "close:elasticsearch",
            "close:mysql",
            "close:redis",
            "application.lifecycle.stopped",
            "complete",
        ],
    )


async def test_stop_groups_cancelled_error_with_ordinary_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    """包含取消的多个关闭失败使用 BaseExceptionGroup 保留原始对象。"""
    events: list[str] = []
    cancelled = CancelledError("tei close cancelled")
    failed = RuntimeError("mysql close failed")
    _patch_api_plan(
        monkeypatch,
        events,
        close_errors={"tei": cancelled, "mysql": failed},
    )
    handle = await runtime.start(runtime.RuntimeRole.API, FastAPI().state)

    try:
        await runtime.stop(handle)
    except BaseExceptionGroup as error:
        check_equal("基础异常组对象顺序", error.exceptions, (cancelled, failed))
    else:
        fail_check(
            "多种关闭基础异常",
            actual="未抛出异常",
            expected="BaseExceptionGroup",
        )
    check_equal("基础异常组后仍排空日志", events[-1], "complete")


async def test_stop_rejects_repeated_and_invalid_handles(
    monkeypatch: MonkeyPatch,
) -> None:
    """Stop 明确拒绝重复关闭和非 start 创建的 handle。"""
    events: list[str] = []
    _patch_api_plan(monkeypatch, events)
    handle = await runtime.start(runtime.RuntimeRole.API, FastAPI().state)
    await runtime.stop(handle)

    for label, candidate in (
        ("重复 handle", handle),
        ("无效 handle", cast(runtime.RuntimeHandle, object())),
    ):
        try:
            await runtime.stop(candidate)
        except RuntimeError as error:
            check_exception(label, error, RuntimeError)
        else:
            fail_check(label, actual="未抛出异常", expected="RuntimeError")


async def test_entry_points_delegate_to_runtime_interface(
    monkeypatch: MonkeyPatch,
) -> None:
    """API lifespan 与 Worker 回调仅持有并传递统一 handle。"""
    api_handle = cast(runtime.RuntimeHandle, object())
    api_start = AsyncMock(return_value=api_handle)
    api_stop = AsyncMock()
    monkeypatch.setattr(application, "start", api_start)
    monkeypatch.setattr(application, "stop", api_stop)
    app = FastAPI()

    async with application._lifespan(app):
        pass

    api_start.assert_awaited_once_with(runtime.RuntimeRole.API, app.state)
    api_stop.assert_awaited_once_with(api_handle)

    worker_handle = object.__new__(runtime.RuntimeHandle)
    worker_start = AsyncMock(return_value=worker_handle)
    worker_stop = AsyncMock()
    monkeypatch.setattr(lifecycle, "start", worker_start)
    monkeypatch.setattr(lifecycle, "stop", worker_stop)
    ctx: dict[Any, Any] = {}
    await lifecycle.startup(ctx)
    check_equal(
        "Worker 保存内部 handle",
        ctx["_runtime_handle"] is worker_handle,
        True,
    )
    try:
        await lifecycle.startup(ctx)
    except RuntimeError as error:
        check_exception("Worker 重复 startup", error, RuntimeError)
    else:
        fail_check("Worker 重复 startup", actual="未抛出异常", expected="RuntimeError")
    worker_start.assert_awaited_once()
    await lifecycle.shutdown(ctx)
    worker_stop.assert_awaited_once_with(worker_handle)
    check_equal("Worker shutdown 移除内部 handle", "_runtime_handle" in ctx, False)
