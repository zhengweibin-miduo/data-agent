"""专用 SELECT-only DW EXPLAIN 与流式执行适配器。"""

import asyncio
import base64
import json
from collections.abc import AsyncGenerator, Awaitable
from typing import TypeVar

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from errors import DataAgentError
from query.application.contracts import (
    QueryBatch,
    QueryExplainRejected,
)
from query.domain import SQLValidationIssue, ValidatedQuery

_Result = TypeVar("_Result")
_CLEANUP_TIMEOUT_SECONDS = 2.0


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

    async def explain(self, query: ValidatedQuery) -> None:
        """在只读事务中预检 SQL；语法对象错误转为稳定修复问题。"""
        try:
            async with asyncio.timeout(self._timeout_seconds):
                async with self._engine.connect() as connection:
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

    async def execute(self, query: ValidatedQuery) -> AsyncGenerator[QueryBatch, None]:
        """在一个只读事务中按行数与字节双预算读取完整结果。"""
        remaining = [self._timeout_seconds]
        connection = self._engine.connect()
        connected = False
        result = None
        try:
            await _within_budget(connection.start(), remaining)
            connected = True
            await _within_budget(
                connection.exec_driver_sql("SET TRANSACTION READ ONLY"), remaining
            )
            result = await _within_budget(
                connection.stream(text(query.sql), query.params), remaining
            )
            columns = list(result.keys())
            pending: list[list[object]] = []
            pending_bytes = 0
            # 驱动按配置行数批量取数，进程内仍逐行执行字节预算切分。
            # 驱动批次还需受字节预算保护；在未知结果宽度时使用保守硬上限，
            # 避免数百个大字段在逐行门禁前同时物化。
            driver_fetch_rows = min(self._fetch_batch_rows, 16)
            partitions = result.partitions(driver_fetch_rows).__aiter__()
            while True:
                try:
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
                    if pending and (
                        len(pending) >= self._fetch_batch_rows
                        or pending_bytes + row_bytes + 1024 > self._max_batch_bytes
                    ):
                        yield QueryBatch(columns=columns, rows=pending)
                        pending = []
                        pending_bytes = 0
                    pending.append(values)
                    pending_bytes += row_bytes
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
                if connected:
                    try:
                        async with asyncio.timeout(_CLEANUP_TIMEOUT_SECONDS):
                            await connection.close()
                    except (Exception, asyncio.CancelledError):
                        await connection.invalidate()

    async def close(self) -> None:
        """关闭专用查询连接池。"""
        await self._engine.dispose()
