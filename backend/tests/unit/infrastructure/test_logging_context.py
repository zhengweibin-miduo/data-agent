"""日志 AOP 上下文、边界包装与 HTTP 中间件检查。"""

import asyncio
import inspect
import json
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from loguru import logger
from pydantic import BaseModel
from pytest import MonkeyPatch
from starlette.types import Message, Receive, Scope, Send

import app_logging as logging_module
from app_logging import (
    RequestLoggingContextMiddleware,
    logging_boundary,
    logging_context,
    setup_logging,
)
from settings import (
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


def _logging_config(log_dir: Path) -> LoggingSettings:
    """创建只写 JSON 文件的测试日志配置。"""
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
            format="json",
            path=log_dir,
            rotation="10 MB",
            retention="7 days",
        ),
    )


@asynccontextmanager
async def _logging_output() -> AsyncGenerator[Path]:
    """建立隔离日志文件，并在退出前排空和移除 Loguru sink。"""
    with TemporaryDirectory() as directory:
        log_dir = Path(directory) / "logs"
        setup_logging(_logging_config(log_dir))
        try:
            yield log_dir
        finally:
            await logger.complete()
            logger.remove()


async def _read_records(log_dir: Path) -> list[dict[str, object]]:
    """排空队列并读取日志文件中的 JSON 记录。"""
    await logger.complete()
    return [
        json.loads(line)
        for line in (log_dir / "data-agent.log")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


async def test_plain_business_log_inherits_and_resets_nested_context() -> None:
    """业务日志无需 bind 即继承上下文，嵌套退出后精确恢复。"""
    async with _logging_output() as log_dir:
        with logging_context(
            trace_id="trace-outer",
            component="test.outer",
            operation="outer_operation",
        ):
            logger.warning("外层日志")
            with logging_context(
                trace_id="trace-inner",
                operation="inner_operation",
            ):
                logger.warning("内层日志")
            logger.warning("恢复外层日志")
        logger.warning("上下文外日志")

        records = await _read_records(log_dir)
        by_message = {str(record["message"]): record for record in records}
        check_equal(
            "业务 warning 自动继承外层上下文",
            (
                by_message["外层日志"]["trace_id"],
                by_message["外层日志"]["component"],
                by_message["外层日志"]["operation"],
            ),
            ("trace-outer", "test.outer", "outer_operation"),
        )
        check_equal(
            "嵌套上下文只覆盖指定字段",
            (
                by_message["内层日志"]["trace_id"],
                by_message["内层日志"]["component"],
                by_message["内层日志"]["operation"],
            ),
            ("trace-inner", "test.outer", "inner_operation"),
        )
        check_equal(
            "嵌套退出恢复外层 token",
            (
                by_message["恢复外层日志"]["trace_id"],
                by_message["恢复外层日志"]["operation"],
            ),
            ("trace-outer", "outer_operation"),
        )
        check_equal(
            "最外层退出后由 Loguru record 派生调用位置",
            (
                by_message["上下文外日志"]["trace_id"],
                by_message["上下文外日志"]["component"],
                by_message["上下文外日志"]["operation"],
            ),
            (
                "-",
                __name__,
                "test_plain_business_log_inherits_and_resets_nested_context",
            ),
        )


async def test_logging_context_isolated_across_concurrent_tasks() -> None:
    """并发 asyncio 任务各自保留 trace、组件和操作上下文。"""
    both_entered = asyncio.Event()
    entered_count = 0
    entered_lock = asyncio.Lock()

    async def emit(
        name: Literal["a", "b"],
        trace_id: str,
    ) -> None:
        """进入独立上下文，待两个任务重叠后再记录。"""
        nonlocal entered_count
        with logging_context(
            trace_id=trace_id,
            component=f"test.{name}",
            operation=f"operation_{name}",
        ):
            async with entered_lock:
                entered_count += 1
                if entered_count == 2:
                    both_entered.set()
            await both_entered.wait()
            logger.warning(f"并发任务 {name}")
            await asyncio.sleep(0)
            logger.warning(f"并发任务 {name} 完成")

    async with _logging_output() as log_dir:
        await asyncio.gather(
            emit("a", "trace-a"),
            emit("b", "trace-b"),
        )
        records = await _read_records(log_dir)

        for name, trace_id in (("a", "trace-a"), ("b", "trace-b")):
            task_records = [
                record
                for record in records
                if str(record["message"]).startswith(f"并发任务 {name}")
            ]
            check_equal(
                f"并发任务 {name} 的上下文不串写",
                {
                    (
                        record["trace_id"],
                        record["component"],
                        record["operation"],
                    )
                    for record in task_records
                },
                {(trace_id, f"test.{name}", f"operation_{name}")},
            )


async def test_except_logs_add_safe_exception_metadata_automatically() -> None:
    """Except 内 warning 自动带异常类型，error 自动带安全堆栈。"""
    secret = "credential-must-not-appear"
    async with _logging_output() as log_dir:
        try:
            raise RuntimeError(secret)
        except RuntimeError:
            logger.warning("可恢复错误")
            logger.error("终止错误")

        records = await _read_records(log_dir)
        warning_record, error_record = records
        check_equal(
            "warning 自动补充异常类型",
            warning_record["error_type"],
            "RuntimeError",
        )
        check_equal(
            "warning 不输出异常堆栈",
            "stack_trace" in warning_record,
            False,
        )
        check_equal(
            "error 自动补充异常类型",
            error_record["error_type"],
            "RuntimeError",
        )
        check_condition(
            "error 自动输出清洗后的堆栈",
            "RuntimeError: <message omitted>" in str(error_record["stack_trace"]),
            actual=error_record.get("stack_trace"),
            expected="包含异常类型和省略后的异常消息",
        )
        check_condition(
            "安全堆栈不泄漏异常消息",
            secret not in json.dumps(error_record, ensure_ascii=False),
            actual=error_record,
            expected="不包含异常消息",
        )


async def test_patcher_failure_does_not_break_business_log(
    monkeypatch: MonkeyPatch,
) -> None:
    """Patcher 读取上下文失败时，业务日志调用仍可正常完成。"""

    class _BrokenContext:
        """模拟读取时失败的日志上下文存储。"""

        def get(self) -> object:
            """抛出基础设施内部异常。"""
            raise RuntimeError("patcher unavailable")

    async with _logging_output() as log_dir:
        monkeypatch.setattr(logging_module, "_LOG_CONTEXT", _BrokenContext())
        logger.warning("日志上下文注入失败，业务日志仍按安全默认字段输出")
        records = await _read_records(log_dir)

        check_equal("patcher 失败仍输出业务日志", len(records), 1)
        check_equal(
            "patcher 失败使用安全默认上下文",
            (
                records[0]["trace_id"],
                records[0]["component"],
                records[0]["operation"],
            ),
            ("-", "application", "-"),
        )


async def test_logging_boundary_preserves_callable_behavior() -> None:
    """边界包装保留同步、异步、异步生成器及异常传播行为。"""
    expected_error = RuntimeError("original")

    @logging_boundary(component="test.sync", operation="calculate")
    def calculate(value: int) -> int:
        """返回同步计算结果。"""
        logger.info("同步边界")
        return value + 1

    @logging_boundary(component="test.async", operation="calculate_async")
    async def calculate_async(value: int) -> int:
        """返回异步计算结果。"""
        await asyncio.sleep(0)
        logger.info("异步边界")
        return value * 2

    @logging_boundary(component="test.stream", operation="stream_values")
    async def stream_values(value: int) -> AsyncGenerator[int]:
        """生成两个值并在实际迭代期记录日志。"""
        logger.info("流式边界一")
        yield value
        await asyncio.sleep(0)
        logger.info("流式边界二")
        yield value + 1

    @logging_boundary(component="test.raise", operation="raise_original")
    def raise_original() -> None:
        """传播原始异常对象。"""
        raise expected_error

    async with _logging_output() as log_dir:
        check_equal("同步边界保留返回值", calculate(2), 3)
        check_equal("异步边界保留返回值", await calculate_async(2), 4)
        check_equal(
            "异步生成器边界保留生成值",
            [item async for item in stream_values(2)],
            [2, 3],
        )
        check_equal(
            "异步生成器包装后仍可被识别",
            inspect.isasyncgenfunction(stream_values),
            True,
        )
        try:
            raise_original()
        except RuntimeError as error:
            check_exception("同步边界传播异常类型", error, RuntimeError)
            check_equal("同步边界传播原异常对象", error is expected_error, True)
        else:
            fail_check(
                "同步边界传播异常",
                actual="未抛出异常",
                expected="传播原 RuntimeError",
            )

        records = await _read_records(log_dir)
        contexts = {
            str(record["message"]): (
                record["component"],
                record["operation"],
            )
            for record in records
        }
        check_equal(
            "同步、异步和流式日志均继承边界上下文",
            contexts,
            {
                "同步边界": ("test.sync", "calculate"),
                "异步边界": ("test.async", "calculate_async"),
                "流式边界一": ("test.stream", "stream_values"),
                "流式边界二": ("test.stream", "stream_values"),
            },
        )


async def test_logging_boundary_defaults_and_safely_reflects_context() -> None:
    """零配置边界从签名和受支持容器提取白名单上下文。"""

    class _JobContext(BaseModel):
        """模拟 Pydantic 任务上下文。"""

        job_id: str
        revision: int
        secret: str

    @dataclass(slots=True)
    class _TaskContext:
        """模拟 slots dataclass 任务上下文。"""

        task_id: str
        attempt: int
        password: str

    @logging_boundary()
    async def observed(
        job: _JobContext,
        metadata: Mapping[str, object],
        task: _TaskContext,
    ) -> str:
        """记录一条无需手工组件、操作或 factory 的业务日志。"""
        logger.info("零配置边界自动提取上下文")
        return "completed"

    async with _logging_output() as log_dir:
        result = await observed(
            _JobContext(job_id="job-1", revision=7, secret="model-secret"),
            {"worker_role": "ddl_metadata", "ignored": "mapping-secret"},
            _TaskContext(task_id="task-1", attempt=3, password="dataclass-secret"),
        )
        records = await _read_records(log_dir)

    check_equal("零配置边界保留业务返回值", result, "completed")
    check_equal("零配置边界只生成一条业务日志", len(records), 1)
    record = records[0]
    check_equal(
        "零配置边界从 callable 派生组件",
        record["component"],
        __name__,
    )
    check_equal(
        "零配置边界从 callable 派生限定操作名",
        record["operation"],
        observed.__qualname__,
    )
    check_equal(
        "白名单反射提取 Pydantic Mapping 和 dataclass 字段",
        (
            record["trace_id"],
            record["job_id"],
            record["revision"],
            record["task_id"],
            record["attempt"],
            record["worker_role"],
        ),
        ("job-1", "job-1", 7, "task-1", 3, "ddl_metadata"),
    )
    serialized = json.dumps(record, ensure_ascii=False)
    check_condition(
        "安全反射不输出非白名单业务字段",
        all(
            secret not in serialized
            for secret in ("model-secret", "mapping-secret", "dataclass-secret")
        ),
        actual=record,
        expected="不包含 Pydantic Mapping 或 dataclass 的非白名单内容",
    )


async def test_logging_boundary_ignores_factory_and_property_failures() -> None:
    """Factory 或任意对象 property 失败不改变业务执行和日志调用。"""

    class _PropertyTrap:
        """访问 property 时抛出异常的非白名单任意对象。"""

        @property
        def job_id(self) -> str:
            """禁止日志反射访问的属性。"""
            raise RuntimeError("property-must-not-be-read")

    def broken_factory(_payload: object) -> Mapping[str, object]:
        """模拟失败的可选上下文 factory。"""
        raise RuntimeError("factory unavailable")

    @logging_boundary(context_factory=broken_factory)
    def observed(payload: object) -> int:
        """记录日志并返回稳定业务结果。"""
        del payload
        logger.info("上下文提取失败不影响业务执行")
        return 42

    async with _logging_output() as log_dir:
        result = observed(_PropertyTrap())
        records = await _read_records(log_dir)

    check_equal("Factory 和 property 失败仍保留业务返回值", result, 42)
    check_equal("上下文提取失败仍输出业务日志", len(records), 1)
    check_equal(
        "失败的可选 factory 不注入任务字段",
        "job_id" in records[0],
        False,
    )


async def test_async_generator_boundary_forwards_control_and_closes_iterator() -> None:
    """异步生成器边界转发 asend/athrow，并在外层关闭时立即清理原迭代器。"""
    closed = asyncio.Event()

    @logging_boundary(component="test.stream", operation="interactive_stream")
    async def interactive_stream() -> AsyncGenerator[int, int]:
        """接收调用方输入，并处理调用方注入的异常。"""
        try:
            try:
                received = yield 1
                yield received
            except ValueError:
                yield 99
        finally:
            logger.info("流式迭代器已关闭，底层资源已经释放")
            closed.set()

    async with _logging_output() as log_dir:
        iterator = interactive_stream()
        check_equal("异步生成器首次迭代值", await anext(iterator), 1)
        check_equal("异步生成器转发 asend", await iterator.asend(7), 7)
        check_equal(
            "异步生成器转发 athrow",
            await iterator.athrow(ValueError("consumer")),
            99,
        )
        await iterator.aclose()
        check_equal("外层 aclose 立即关闭原迭代器", closed.is_set(), True)

        records = await _read_records(log_dir)
        close_record = records[-1]
        check_equal(
            "关闭清理日志保留流式边界上下文",
            (close_record["component"], close_record["operation"]),
            ("test.stream", "interactive_stream"),
        )


async def test_async_boundary_preserves_cancellation_object() -> None:
    """异步边界原样传播取消对象，不转换为普通失败。"""
    expected_error = asyncio.CancelledError("cancelled")

    @logging_boundary(component="test.async", operation="cancel")
    async def cancel() -> None:
        """抛出调用方可识别的取消对象。"""
        raise expected_error

    try:
        await cancel()
    except asyncio.CancelledError as error:
        check_equal("异步边界传播原取消对象", error is expected_error, True)
    else:
        fail_check(
            "异步边界传播取消",
            actual="未抛出异常",
            expected="传播原 CancelledError",
        )


def _http_scope(path: str) -> Scope:
    """构造最小且完整的 HTTP ASGI scope。"""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "scheme": "http",
        "method": "GET",
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "state": {},
    }


async def test_request_middleware_covers_stream_and_isolates_requests() -> None:
    """HTTP 中间件覆盖完整流式响应，且并发请求不共享 trace。"""
    request_started = {"/a": asyncio.Event(), "/b": asyncio.Event()}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        """发送两个响应分片，并让两个请求的流式阶段重叠。"""
        del receive
        path = scope["path"]
        logger.info(f"{path} 请求")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            }
        )
        logger.info(f"{path} 流分片一")
        await send(
            {
                "type": "http.response.body",
                "body": b"first",
                "more_body": True,
            }
        )
        request_started[path].set()
        other_path = "/b" if path == "/a" else "/a"
        await request_started[other_path].wait()
        logger.info(f"{path} 流分片二")
        await send(
            {
                "type": "http.response.body",
                "body": b"second",
                "more_body": False,
            }
        )

    async def receive() -> Message:
        """返回一个空 HTTP 请求消息。"""
        return {"type": "http.request", "body": b"", "more_body": False}

    middleware = RequestLoggingContextMiddleware(app)
    sent: dict[str, list[Message]] = {"/a": [], "/b": []}

    async def invoke(path: Literal["/a", "/b"]) -> None:
        """调用一次中间件并记录下游 ASGI 消息。"""

        async def send(message: Message) -> None:
            """记录指定请求发出的 ASGI 消息。"""
            sent[path].append(message)

        await middleware(_http_scope(path), receive, send)

    async with _logging_output() as log_dir:
        await asyncio.gather(invoke("/a"), invoke("/b"))
        records = await _read_records(log_dir)

        traces_by_path: dict[str, set[str]] = {}
        for path in ("/a", "/b"):
            request_records = [
                record
                for record in records
                if str(record["message"]).startswith(path)
            ]
            traces_by_path[path] = {
                str(record["trace_id"]) for record in request_records
            }
            check_equal(
                f"{path} 请求和全部流分片共享 trace 且从 record 派生调用位置",
                {
                    (
                        record["component"],
                        record["operation"],
                        record["trace_id"] == record["request_id"],
                    )
                    for record in request_records
                },
                {(__name__, "app", True)},
            )
            check_equal(
                f"{path} 流式响应内容保持不变",
                [
                    message.get("body")
                    for message in sent[path]
                    if message["type"] == "http.response.body"
                ],
                [b"first", b"second"],
            )

        check_equal(
            "每个请求内部只有一个 trace",
            {path: len(traces) for path, traces in traces_by_path.items()},
            {"/a": 1, "/b": 1},
        )
        check_condition(
            "并发请求使用不同 trace",
            traces_by_path["/a"].isdisjoint(traces_by_path["/b"]),
            actual=traces_by_path,
            expected="两个请求的 trace 集合不相交",
        )
