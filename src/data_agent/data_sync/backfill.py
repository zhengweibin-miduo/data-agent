"""按主键分块回填并幂等应用 DW 行变更。"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import and_, column, delete, table, text
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from data_agent.data_sync.models import (
    DesiredSyncTable,
    EncodedValue,
    RowOperation,
    canonical_primary_key,
    decode_row_value,
    encode_row_value,
)
from data_agent.data_sync.repository import (
    BufferedSyncEvent,
    ClaimedSyncTask,
    DataSyncRepository,
)
from data_agent.errors import DataAgentError


async def read_backfill_batch(
    engine: AsyncEngine,
    desired: DesiredSyncTable,
    *,
    after_key: Sequence[object] | None,
    limit: int,
) -> list[dict[str, object]]:
    """使用有序主键游标读取有限源数据。"""
    if limit <= 0:
        return []
    preparer = engine.dialect.identifier_preparer
    quote = preparer.quote
    columns = ", ".join(quote(item.name) for item in desired.columns)
    qualified = f"{quote(desired.source_schema)}.{quote(desired.source_table)}"
    order_by = ", ".join(quote(name) for name in desired.primary_key)
    parameters: dict[str, object] = {"limit": limit}
    where = ""
    if after_key is not None:
        if len(after_key) != len(desired.primary_key):
            raise ValueError("回填主键游标长度与目标主键不一致")
        names = ", ".join(quote(name) for name in desired.primary_key)
        placeholders = ", ".join(
            f":backfill_key_{index}" for index in range(len(after_key))
        )
        where = f" WHERE ({names}) > ({placeholders})"
        parameters.update(
            {f"backfill_key_{index}": value for index, value in enumerate(after_key)}
        )
    statement = text(
        f"SELECT {columns} FROM {qualified}{where} ORDER BY {order_by} LIMIT :limit"
    )
    async with engine.connect() as connection:
        result = await connection.execute(statement, parameters)
        return [dict(row) for row in result.mappings()]


async def apply_backfill_batch(
    session: AsyncSession,
    task: ClaimedSyncTask,
    rows: Sequence[Mapping[str, object]],
    *,
    dw_database: str,
) -> tuple[object, ...] | None:
    """在一个目标事务内写入回填批次并推进主键游标。"""
    if not rows:
        return None
    repository = DataSyncRepository(session)
    values = [_desired_values(task.desired, row) for row in rows]
    for row in values:
        await _claim_owner(repository, task.desired, row)
    await session.execute(_upsert_statement(task.desired, values, dw_database))
    last_key = tuple(values[-1][name] for name in task.desired.primary_key)
    if not await repository.record_backfill_cursor(task, last_key):
        raise RuntimeError("回填批次完成后同步任务租约已失效")
    return last_key


async def reset_source_rows(
    session: AsyncSession,
    task: ClaimedSyncTask,
    *,
    dw_database: str,
) -> None:
    """新 generation 建立基线前删除该来源旧行并保留永久归属。"""
    repository = DataSyncRepository(session)
    documents = await repository.source_key_documents(
        target_table=task.desired.target_table,
        source=task.desired.source,
    )
    for document in documents:
        encoded = json.loads(document)
        row = {
            name: decode_row_value(encoded[name]) for name in task.desired.primary_key
        }
        await session.execute(_delete_statement(task.desired, row, dw_database))


async def apply_buffered_event(
    session: AsyncSession,
    task: ClaimedSyncTask,
    buffered: BufferedSyncEvent,
    *,
    dw_database: str,
) -> None:
    """原子应用一个 Binlog 行事件、确认事件并推进位点。"""
    event = buffered.event
    desired = task.desired
    if (
        event.source != desired.source
        or event.source_schema != desired.source_schema
        or event.source_table != desired.source_table
    ):
        raise DataAgentError(
            "unexpected_binlog_event",
            "data_sync",
            "暂存事件不属于当前同步任务",
            details={"task_id": str(task.id)},
        )
    repository = DataSyncRepository(session)
    if event.operation == RowOperation.DELETE:
        before = _decoded_event_row(desired, event.before)
        await _claim_owner(repository, desired, before)
        await session.execute(_delete_statement(desired, before, dw_database))
        key_document, key_hash = _primary_key_identity(desired, before)
        if not await repository.tombstone_key_owner(
            target_table=desired.target_table,
            primary_key_hash=key_hash,
            source=desired.source,
        ):
            raise RuntimeError("删除事件未找到当前来源拥有的目标主键")
    else:
        after = _decoded_event_row(desired, event.after)
        if event.operation == RowOperation.UPDATE and event.before is not None:
            before = _decoded_event_row(desired, event.before)
            if _primary_key_values(desired, before) != _primary_key_values(
                desired, after
            ):
                await _claim_owner(repository, desired, before)
                await session.execute(_delete_statement(desired, before, dw_database))
                _, old_hash = _primary_key_identity(desired, before)
                await repository.tombstone_key_owner(
                    target_table=desired.target_table,
                    primary_key_hash=old_hash,
                    source=desired.source,
                )
        await _claim_owner(repository, desired, after)
        await session.execute(_upsert_statement(desired, [after], dw_database))
    if not await repository.acknowledge_event(task.id, buffered.id):
        raise RuntimeError("Binlog 事件确认失败")
    if not await repository.advance_applied_coordinate(task, event.coordinate):
        raise RuntimeError("Binlog 事件应用后同步位点未推进")


def _desired_values(
    desired: DesiredSyncTable,
    row: Mapping[str, object],
) -> dict[str, object]:
    """投影并校验一行已接受字段。"""
    missing = [column.name for column in desired.columns if column.name not in row]
    if missing:
        raise DataAgentError(
            "source_row_missing_columns",
            "data_sync",
            "源数据缺少已接受字段",
            details={"columns": ",".join(missing[:20])},
        )
    return {column.name: row[column.name] for column in desired.columns}


def _decoded_event_row(
    desired: DesiredSyncTable,
    encoded: Mapping[str, EncodedValue] | None,
) -> dict[str, object]:
    """解码并投影一个 Binlog 行镜像。"""
    if encoded is None:
        raise DataAgentError(
            "incomplete_binlog_row",
            "data_sync",
            "Binlog 行镜像不完整",
        )
    return _desired_values(
        desired,
        {name: decode_row_value(value) for name, value in encoded.items()},
    )


async def _claim_owner(
    repository: DataSyncRepository,
    desired: DesiredSyncTable,
    row: Mapping[str, object],
) -> None:
    """建立目标主键归属并把跨源碰撞收敛为确定性冲突。"""
    document, key_hash = _primary_key_identity(desired, row)
    conflict = await repository.claim_key_owner(
        target_table=desired.target_table,
        primary_key_hash=key_hash,
        primary_key_json=document,
        source=desired.source,
    )
    if conflict is not None:
        raise DataAgentError(
            "dw_primary_key_conflict",
            "data_sync",
            "DW 目标主键已由其他数据源占用",
            details={
                "table": conflict.target_table,
                "owner_source": conflict.owner_source,
                "contender_source": conflict.contender_source,
            },
        )


def _primary_key_identity(
    desired: DesiredSyncTable,
    row: Mapping[str, object],
) -> tuple[str, str]:
    """编码目标主键及其稳定哈希。"""
    encoded: dict[str, EncodedValue] = {}
    columns = {column.name: column for column in desired.columns}
    for name in desired.primary_key:
        value = row[name]
        if columns[name].data_type.strip().upper().startswith("BIT"):
            if isinstance(value, bytes):
                value = int.from_bytes(value, byteorder="big", signed=False)
            elif not isinstance(value, int):
                raise TypeError("MySQL BIT 主键必须解码为 bytes 或 int")
        encoded[name] = encode_row_value(value)
    return canonical_primary_key(desired.primary_key, encoded)


def _primary_key_values(
    desired: DesiredSyncTable,
    row: Mapping[str, object],
) -> tuple[object, ...]:
    """返回有序目标主键值。"""
    return tuple(row[name] for name in desired.primary_key)


def _target_table(desired: DesiredSyncTable, dw_database: str) -> Any:
    """构建只包含已接受字段的动态 SQLAlchemy 表子句。"""
    return table(
        desired.target_table,
        *(column(item.name) for item in desired.columns),
        schema=dw_database,
    )


def _upsert_statement(
    desired: DesiredSyncTable,
    rows: Sequence[Mapping[str, object]],
    dw_database: str,
) -> Any:
    """构建按已接受主键收敛的 MySQL upsert。"""
    target = _target_table(desired, dw_database)
    statement = insert(target).values(list(rows))
    updates = {
        item.name: statement.inserted[item.name]
        for item in desired.columns
        if item.name not in desired.primary_key
    }
    if not updates:
        primary_key = desired.primary_key[0]
        updates[primary_key] = statement.inserted[primary_key]
    return statement.on_duplicate_key_update(**updates)


def _delete_statement(
    desired: DesiredSyncTable,
    row: Mapping[str, object],
    dw_database: str,
) -> Any:
    """构建按完整主键删除一个 DW 行的语句。"""
    target = _target_table(desired, dw_database)
    return delete(target).where(
        and_(*(target.c[name] == row[name] for name in desired.primary_key))
    )
