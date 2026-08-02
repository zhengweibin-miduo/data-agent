"""把具体 MySQL Binlog 源投影为 Data Sync source port。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from data_sync.application.contracts import CapturedEvents
from data_sync.backfill import read_backfill_batch
from data_sync.binlog import MySQLSourceClient
from data_sync.models import BinlogCoordinate, DesiredSyncTable


class MySQLSourceAdapter:
    """隐藏 SQLAlchemy engine 和 Binlog client implementation 的源适配器。"""

    def __init__(self, client: MySQLSourceClient) -> None:
        """保存 composition root 创建的具体源客户端。"""
        self._client = client

    async def check_select_access(self, source_schema: str, source_table: str) -> None:
        """确认源表可被只读账号访问。"""
        await self._client.check_select_access(source_schema, source_table)

    async def current_coordinate(self) -> BinlogCoordinate:
        """读取当前安全 Binlog 位点。"""
        return await self._client.current_coordinate()

    async def capture(
        self,
        *,
        source_schema: str,
        source_table: str,
        start: BinlogCoordinate,
        limit: int,
        byte_limit: int,
    ) -> CapturedEvents:
        """捕获有界事件并移除具体 client result 类型。"""
        captured = await self._client.capture(
            source_schema=source_schema,
            source_table=source_table,
            start=start,
            limit=limit,
            byte_limit=byte_limit,
        )
        return CapturedEvents(events=captured.events, tail=captured.tail)

    async def read_backfill_batch(
        self,
        desired: DesiredSyncTable,
        *,
        after_key: Sequence[object] | None,
        limit: int,
    ) -> list[Mapping[str, object]]:
        """通过具体源 engine 执行有界主键 keyset 读取。"""
        rows = await read_backfill_batch(
            self._client.engine,
            desired,
            after_key=after_key,
            limit=limit,
        )
        return list(rows)
