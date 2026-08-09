"""专用 SELECT-only DW EXPLAIN 与流式执行适配器。"""

import asyncio
import base64
import json
from collections.abc import AsyncGenerator, Awaitable, Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TypeVar, cast

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from errors import DataAgentError
from infrastructure.mysql import AdvisoryLockUnavailableError
from query.application.contracts import (
    QueryBatch,
    QueryExplainRejected,
)
from query.domain import SQLValidationIssue, ValidatedQuery

_Result = TypeVar("_Result")
_CLEANUP_TIMEOUT_SECONDS = 2.0
_GENERATION_LOCK_NAMESPACE = "data-agent-generation-v1"


async def _within_budget(
    awaitable: Awaitable[_Result], remaining: list[float]
) -> _Result:
    """只累计数据库 I/O 等待时间，响应背压不消耗执行预算。"""
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        async with asyncio.timeout(remaining[0]):
            return await awaitable
    finally:
        remaining[0] = max(0.0, remaining[0] - (loop.time() - started))


def _stream_value(value: object) -> object:
    """把驱动二进制值转换为不会破坏 UTF-8 NDJSON 的稳定文本。"""
    if isinstance(value, (bytes, bytearray, memoryview)):
        encoded = base64.b64encode(bytes(value)).decode("ascii")
        return f"base64:{encoded}"
    if isinstance(value, list):
        return [_stream_value(item) for item in value]
    if isinstance(value, tuple):
        return [_stream_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _stream_value(item) for key, item in value.items()}
    return value


class MySQLQueryExecutor:
    """拥有独立只读账号连接池且不暴露任意语句或提交 interface。"""

    def __init__(
        self,
        read_url: str,
        *,
        timeout_seconds: float,
        fetch_batch_rows: int,
        max_batch_bytes: int,
    ) -> None:
        """创建专用 DW 查询引擎并绑定执行与单批预算。"""
        self._engine: AsyncEngine = create_async_engine(
            read_url,
            pool_pre_ping=True,
            pool_recycle=3600,
            connect_args={"init_command": "SET time_zone = '+00:00'"},
        )
        self._timeout_seconds = timeout_seconds
        self._fetch_batch_rows = fetch_batch_rows
        self._max_batch_bytes = max_batch_bytes
        self._generation_connection: ContextVar[AsyncConnection | None] = ContextVar(
            "query_generation_connection", default=None
        )

    @asynccontextmanager
    async def hold_generation(self, names: tuple[str, ...], timeout_seconds: int):
        """在同一只读连接上持锁和执行，owner 断线会原子终止查询。"""
        connection = self._engine.connect()
        await connection.start()
        await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        token = self._generation_connection.set(connection)
        arguments = ", ".join(f":name_{index}" for index in range(len(names)))
        params: dict[str, object] = {
            "namespace": _GENERATION_LOCK_NAMESPACE,
            "timeout": timeout_seconds,
            **{f"name_{index}": name for index, name in enumerate(names)},
        }
        acquired = False
        try:
            async with asyncio.timeout(timeout_seconds + self._timeout_seconds):
                acquired = bool(
                    await connection.scalar(
                        text(
                            "SELECT service_get_read_locks(:namespace, "
                            f"{arguments}, :timeout)"
                        ),
                        params,
                    )
                )
            if not acquired:
                raise AdvisoryLockUnavailableError(
                    "MySQL generation lock 未在等待预算内取得"
                )
            yield
        except TimeoutError as error:
            await connection.invalidate()
            raise AdvisoryLockUnavailableError(
                "MySQL generation lock 网络 I/O 超时"
            ) from error
        finally:
            self._generation_connection.reset(token)
            if acquired:
                try:
                    async with asyncio.timeout(_CLEANUP_TIMEOUT_SECONDS):
                        await connection.scalar(
                            text("SELECT service_release_locks(:namespace)"),
                            {"namespace": _GENERATION_LOCK_NAMESPACE},
                        )
                except BaseException:
                    await connection.invalidate()
            await connection.close()

    async def explain(self, query: ValidatedQuery) -> None:
        """在只读事务中预检 SQL；语法对象错误转为稳定修复问题。"""
        try:
            async with asyncio.timeout(self._timeout_seconds):
                bound = self._generation_connection.get()
                connection_context = (
                    self._engine.connect() if bound is None else _borrowed(bound)
                )
                async with connection_context as connection:
                    if bound is None:
                        await connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                    await connection.execute(text(f"EXPLAIN {query.sql}"), query.params)
                    await connection.rollback()
        except ProgrammingError as error:
            raise QueryExplainRejected(
                SQLValidationIssue(code="explain_rejected")
            ) from error
        except TimeoutError as error:
            raise DataAgentError(
                "query_timeout",
                "query_explain",
                "DW 查询预检超过执行预算",
                http_status=504,
            ) from error
        except SQLAlchemyError as error:
            raise DataAgentError(
                "query_database_failed",
                "query_explain",
                "DW 查询预检连接失败",
                retryable=True,
                http_status=502,
            ) from error

    async def ensure_timezone_supported(self, timezone: str) -> None:
        """证明 MySQL named-zone 表可解析本次用户时区。"""
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._engine.connect() as connection:
                    supported = await connection.scalar(
                        text(
                            "SELECT CONVERT_TZ("
                            "'2000-01-01 00:00:00', '+00:00', :timezone) IS NOT NULL"
                        ),
                        {"timezone": timezone},
                    )
                    if supported != 1:
                        raise DataAgentError(
                            "query_timezone_unsupported",
                            "query_validation",
                            "DW 查询实例未加载请求时区数据",
                            retryable=False,
                            http_status=422,
                        )
        except TimeoutError as error:
            raise DataAgentError(
                "query_timeout",
                "query_validation",
                "DW 时区能力检查超过执行预算",
                http_status=504,
            ) from error
        except SQLAlchemyError as error:
            raise DataAgentError(
                "query_database_failed",
                "query_validation",
                "DW 时区能力检查失败",
                retryable=True,
                http_status=502,
            ) from error
    async def execute(self, query: ValidatedQuery) -> AsyncGenerator[QueryBatch, None]:
        """在一个只读事务中按行数与字节双预算读取完整结果。"""
        remaining = [self._timeout_seconds]
        bound = self._generation_connection.get()
        connection = self._engine.connect() if bound is None else bound
        connected = bound is not None
        owned = bound is None
        result = None
        try:
            if owned:
                await _within_budget(connection.start(), remaining)
                connected = True
            if owned:
                await _within_budget(
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY"), remaining
                )
            result = await _within_budget(
                connection.stream(text(query.sql), query.params), remaining
            )
            columns = list(result.keys())
            pending: list[list[object]] = []
            pending_bytes = 0
            # 首行单独读取以建立保守行宽上界；通过字节门禁后，再按该上界
            # 与配置行数选择驱动批次，避免永久退化为逐行游标 await。
            driver_fetch_rows = 1
            max_observed_row_bytes = 0
            fetchmany = getattr(result, "fetchmany", None)
            partitions = (
                None
                if callable(fetchmany)
                else result.partitions(driver_fetch_rows).__aiter__()
            )
            while True:
                try:
                    if callable(fetchmany):
                        partition = await _within_budget(
                            cast(
                                Awaitable[list[Sequence[object]]],
                                fetchmany(driver_fetch_rows),
                            ),
                            remaining,
                        )
                        if not partition:
                            break
                    else:
                        assert partitions is not None
                        partition = await _within_budget(anext(partitions), remaining)
                except StopAsyncIteration:
                    break
                for row in partition:
                    values = [_stream_value(value) for value in row]
                    row_bytes = len(
                        json.dumps(
                            values,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        ).encode()
                    )
                    if row_bytes + 1024 > self._max_batch_bytes:
                        raise DataAgentError(
                            "query_row_too_large",
                            "query_execute",
                            "单行结果超过流式批次字节预算",
                            http_status=422,
                        )
                    max_observed_row_bytes = max(max_observed_row_bytes, row_bytes)
                    if pending and (
                        len(pending) >= self._fetch_batch_rows
                        or pending_bytes + row_bytes + 1024 > self._max_batch_bytes
                    ):
                        yield QueryBatch(columns=columns, rows=pending)
                        pending = []
                        pending_bytes = 0
                    pending.append(values)
                    pending_bytes += row_bytes
                    driver_fetch_rows = min(
                        self._fetch_batch_rows,
                        # 未知的下一行可能突然变宽。驱动层采用很小的有界预取，
                        # 采用 1→2→4 的保守增长，而不是永久停在两行或因一条
                        # 窄行直接放大到配置上限。四行硬上限将未知宽行的门禁前
                        # 物化量保持有界，同时恢复真正的批量游标读取。
                        min(driver_fetch_rows * 2, 4),
                        max(
                            1,
                            self._max_batch_bytes
                            // (max_observed_row_bytes + 1024),
                        ),
                    )
            # 空结果仍需要把数据库返回的字段名交给 metadata 事件。
            yield QueryBatch(columns=columns, rows=pending)
        except TimeoutError as error:
            raise DataAgentError(
                "query_timeout",
                "query_execute",
                "DW 查询执行超过时间预算",
                http_status=504,
            ) from error
        except DataAgentError:
            raise
        except SQLAlchemyError as error:
            raise DataAgentError(
                "query_database_failed",
                "query_execute",
                "DW 查询执行失败",
                retryable=True,
                http_status=502,
            ) from error
        finally:
            try:
                if result is not None:
                    try:
                        async with asyncio.timeout(_CLEANUP_TIMEOUT_SECONDS):
                            await result.close()
                    except (Exception, asyncio.CancelledError):
                        pass
                if connected and connection.in_transaction():
                    try:
                        async with asyncio.timeout(_CLEANUP_TIMEOUT_SECONDS):
                            await connection.rollback()
                    except (Exception, asyncio.CancelledError):
                        await connection.invalidate()
            finally:
                if connected and owned:
                    try:
                        async with asyncio.timeout(_CLEANUP_TIMEOUT_SECONDS):
                            await connection.close()
                    except (Exception, asyncio.CancelledError):
                        await connection.invalidate()

    async def close(self) -> None:
        """关闭专用查询连接池。"""
        await self._engine.dispose()


@asynccontextmanager
async def _borrowed(connection: AsyncConnection):
    """把已由 generation guard 管理的连接适配成上下文。"""
    yield connection
