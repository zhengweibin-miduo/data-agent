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


async def _before_deadline(
    awaitable: Awaitable[_Result], deadline: float
) -> _Result:
    """只在数据库 await 期间执行总截止时间取消。"""
    async with asyncio.timeout_at(deadline):
        return await awaitable


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

    async def execute(
        self, query: ValidatedQuery
    ) -> AsyncGenerator[QueryBatch, None]:
        """在一个只读事务中按行数与字节双预算读取完整结果。"""
        deadline = asyncio.get_running_loop().time() + self._timeout_seconds
        connection = self._engine.connect()
        connected = False
        result = None
        try:
            await _before_deadline(connection.start(), deadline)
            connected = True
            await _before_deadline(
                connection.exec_driver_sql("SET TRANSACTION READ ONLY"), deadline
            )
            result = await _before_deadline(
                connection.stream(text(query.sql), query.params), deadline
            )
            columns = list(result.keys())
            pending: list[list[object]] = []
            pending_bytes = 0
            # 步骤一（ponytail）：逐行取数，避免 500 个近 1 MiB 行先进入内存。
            partitions = result.partitions(1).__aiter__()
            while True:
                try:
                    partition = await _before_deadline(anext(partitions), deadline)
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
            if result is not None:
                await result.close()
            if connected:
                if connection.in_transaction():
                    await connection.rollback()
                await connection.close()

    async def close(self) -> None:
        """关闭专用查询连接池。"""
        await self._engine.dispose()
