"""命名 MySQL 数据源与 Binlog ROW 事件适配器。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, cast

from pymysqlreplication import BinLogStreamReader
from pymysqlreplication.row_event import (
    DeleteRowsEvent,
    UpdateRowsEvent,
    WriteRowsEvent,
)
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from data_agent.data_sync.models import (
    BinlogCoordinate,
    EncodedValue,
    RowOperation,
    SyncRowEvent,
    encode_row_value,
)
from data_agent.settings import DataSyncSourceSettings

_ROW_EVENTS = (WriteRowsEvent, UpdateRowsEvent, DeleteRowsEvent)


class RawRowsEvent(Protocol):
    """Binlog 解码器所需的最小第三方事件协议。"""

    schema: str
    table: str
    rows: list[Mapping[str, Mapping[str, object]]]
    packet: Any


@dataclass(frozen=True, slots=True)
class BinlogCaptureResult:
    """一次有界 Binlog 捕获结果。"""

    events: tuple[SyncRowEvent, ...]
    tail: BinlogCoordinate


class SourceCapabilityError(RuntimeError):
    """源库不满足 ROW/FULL Binlog 前置条件。"""


class MySQLSourceClient:
    """持有一个命名源库的查询引擎和短生命周期 Binlog 读取器。"""

    def __init__(
        self,
        name: str,
        settings: DataSyncSourceSettings,
        *,
        connect_timeout_seconds: int,
        read_timeout_seconds: int,
    ) -> None:
        """保存安全来源标识并创建可复用异步查询引擎。"""
        self.name = name
        self._settings = settings
        self._url = make_url(settings.url)
        self._connect_timeout_seconds = connect_timeout_seconds
        self._read_timeout_seconds = read_timeout_seconds
        self._engine = create_async_engine(
            settings.url,
            pool_pre_ping=True,
            pool_recycle=3600,
            connect_args={
                "connect_timeout": connect_timeout_seconds,
                "read_timeout": read_timeout_seconds,
            },
        )

    @property
    def engine(self) -> AsyncEngine:
        """返回供有界回填读取复用的异步源库引擎。"""
        return self._engine

    async def check_capabilities(self) -> None:
        """确认源库开启 ROW Binlog 和 FULL 行镜像。"""
        # 步骤一：只读取服务器能力变量，不查询或记录任何业务行。
        async with self._engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT @@GLOBAL.binlog_format AS binlog_format, "
                    "@@GLOBAL.binlog_row_image AS binlog_row_image"
                )
            )
            row = result.mappings().one()
        # 步骤二：不满足可恢复事件契约时在进程启动阶段安全失败。
        if str(row["binlog_format"]).upper() != "ROW":
            raise SourceCapabilityError(f"数据源 {self.name} 未启用 ROW Binlog")
        if str(row["binlog_row_image"]).upper() != "FULL":
            raise SourceCapabilityError(f"数据源 {self.name} 未启用 FULL Binlog 行镜像")

    async def current_coordinate(self) -> BinlogCoordinate:
        """读取开始历史回填前的当前 Binlog 位点。"""
        async with self._engine.connect() as connection:
            try:
                result = await connection.execute(text("SHOW BINARY LOG STATUS"))
            except Exception:
                # 步骤一：兼容 MySQL 8.0 旧命令；原始异常由最终失败路径保留。
                result = await connection.execute(text("SHOW MASTER STATUS"))
            row = result.mappings().one()
        return BinlogCoordinate(
            file=str(row["File"]),
            position=int(row["Position"]),
            row_index=0,
        )

    async def capture(
        self,
        *,
        source_schema: str,
        source_table: str,
        start: BinlogCoordinate,
        limit: int,
    ) -> BinlogCaptureResult:
        """从给定位点读取到当前尾部，最多返回 ``limit`` 条规范事件。"""
        if limit <= 0:
            return BinlogCaptureResult(events=(), tail=start)
        return await asyncio.to_thread(
            self._capture_sync,
            source_schema=source_schema,
            source_table=source_table,
            start=start,
            limit=limit,
        )

    async def close(self) -> None:
        """释放该命名源库的查询连接池。"""
        await self._engine.dispose()

    def _capture_sync(
        self,
        *,
        source_schema: str,
        source_table: str,
        start: BinlogCoordinate,
        limit: int,
    ) -> BinlogCaptureResult:
        """在线程中运行同步 Binlog 客户端的一次有限读取。"""
        stream = BinLogStreamReader(
            connection_settings=_replication_connection_settings(
                self._url,
                connect_timeout_seconds=self._connect_timeout_seconds,
                read_timeout_seconds=self._read_timeout_seconds,
            ),
            server_id=self._settings.server_id,
            resume_stream=True,
            blocking=False,
            only_events=list(_ROW_EVENTS),
            only_schemas=[source_schema],
            only_tables=[source_table],
            log_file=start.file,
            log_pos=start.position,
            freeze_schema=True,
            use_column_name_cache=True,
            enable_logging=False,
        )
        events: list[SyncRowEvent] = []
        tail = start
        try:
            for raw_event in stream:
                if stream.log_file is None or stream.log_pos is None:
                    raise RuntimeError("Binlog 事件缺少可恢复文件位点")
                coordinate = BinlogCoordinate(
                    file=str(stream.log_file),
                    position=int(stream.log_pos),
                    row_index=0,
                )
                decoded = decode_rows_event(
                    raw_event,
                    source=self.name,
                    coordinate=coordinate,
                )
                events.extend(decoded)
                if decoded:
                    tail = decoded[-1].coordinate
                else:
                    tail = coordinate
                if len(events) >= limit:
                    break
            if (
                not events
                and stream.log_file is not None
                and stream.log_pos is not None
            ):
                tail = BinlogCoordinate(
                    file=str(stream.log_file),
                    position=max(4, int(stream.log_pos)),
                    row_index=0,
                )
        finally:
            stream.close()
        return BinlogCaptureResult(events=tuple(events), tail=tail)


def decode_rows_event(
    raw_event: object,
    *,
    source: str,
    coordinate: BinlogCoordinate,
) -> list[SyncRowEvent]:
    """把第三方 ROW 事件解码成单一可持久化契约。"""
    event = cast(RawRowsEvent, raw_event)
    if isinstance(raw_event, WriteRowsEvent):
        operation = RowOperation.INSERT
        before_key = None
        after_key = "values"
    elif isinstance(raw_event, UpdateRowsEvent):
        operation = RowOperation.UPDATE
        before_key = "before_values"
        after_key = "after_values"
    elif isinstance(raw_event, DeleteRowsEvent):
        operation = RowOperation.DELETE
        before_key = "values"
        after_key = None
    else:
        raise TypeError(f"不支持的 Binlog 事件类型：{type(raw_event).__name__}")

    decoded: list[SyncRowEvent] = []
    for row_index, row in enumerate(event.rows):
        decoded.append(
            SyncRowEvent(
                source=source,
                source_schema=event.schema,
                source_table=event.table,
                coordinate=BinlogCoordinate(
                    file=coordinate.file,
                    position=coordinate.position,
                    row_index=coordinate.row_index + row_index,
                ),
                operation=operation,
                before=_encode_row(row[before_key]) if before_key is not None else None,
                after=_encode_row(row[after_key]) if after_key is not None else None,
            )
        )
    return decoded


def _encode_row(row: Mapping[str, object]) -> dict[str, EncodedValue]:
    """编码第三方事件中的一行值。"""
    return {name: encode_row_value(value) for name, value in row.items()}


def _replication_connection_settings(
    url: URL,
    *,
    connect_timeout_seconds: int,
    read_timeout_seconds: int,
) -> dict[str, object]:
    """把 SQLAlchemy URL 投影为 mysql-replication 的安全连接参数。"""
    if url.host is None or url.username is None:
        raise ValueError("命名数据源地址必须包含主机和用户名")
    settings: dict[str, object] = {
        "host": url.host,
        "port": url.port or 3306,
        "user": url.username,
        "password": url.password or "",
        "charset": url.query.get("charset", "utf8mb4"),
        "connect_timeout": connect_timeout_seconds,
        "read_timeout": read_timeout_seconds,
    }
    if url.database is not None:
        settings["database"] = url.database
    return settings


async def close_sources(sources: Iterable[MySQLSourceClient]) -> None:
    """关闭全部源连接，并在完成后抛出首个关闭异常。"""
    results = await asyncio.gather(
        *(source.close() for source in sources),
        return_exceptions=True,
    )
    for result in results:
        if isinstance(result, BaseException):
            raise result
