"""数据同步期望状态、位点、行事件与冲突契约。"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import TypeAlias, cast

from pydantic import Field

from data_agent.errors import DataAgentError
from data_agent.models.base import ContractModel
from data_agent.models.physical import PhysicalSchema
from data_agent.models.semantic import MetricMetadata, SemanticMetadata

JSONScalar: TypeAlias = str | int | float | bool | None
EncodedValue: TypeAlias = JSONScalar | dict[str, str]


class SyncPhase(StrEnum):
    """数据同步任务阶段。"""

    PENDING_SCHEMA = "pending_schema"
    BUFFERING = "buffering"
    BACKFILLING = "backfilling"
    REPLAYING = "replaying"
    STREAMING = "streaming"
    PAUSED = "paused"
    CONFLICT = "conflict"
    DEAD = "dead"


class RowOperation(StrEnum):
    """源表行级变更操作。"""

    INSERT = "insert"
    UPDATE = "update"
    DELETE = "delete"


class BinlogCoordinate(ContractModel):
    """可排序且可恢复的 MySQL Binlog 位点。"""

    file: str = Field(min_length=1, max_length=255, description="Binlog 文件名称。")
    position: int = Field(ge=4, description="Binlog 事件结束位置。")
    row_index: int = Field(ge=0, description="同一 Binlog 事件内的行序号。")


class DesiredColumn(ContractModel):
    """由已接受物理模式确定的目标列。"""

    id: str = Field(description="Meta 字段唯一标识。")
    name: str = Field(min_length=1, max_length=64, description="目标字段名称。")
    data_type: str = Field(min_length=1, max_length=255, description="MySQL 字段类型。")
    nullable: bool = Field(description="目标字段是否允许空值。")


class DesiredSyncTable(ContractModel):
    """一张源表到统一 DW 表的期望同步结构。"""

    source: str = Field(min_length=1, max_length=128, description="命名数据源配置键。")
    source_schema: str = Field(
        min_length=1,
        max_length=64,
        description="源 MySQL 数据库名称。",
    )
    source_table: str = Field(
        min_length=1,
        max_length=64,
        description="源 MySQL 表名称。",
    )
    target_table: str = Field(
        min_length=1,
        max_length=64,
        description="不带来源前缀的统一 DW 表名称。",
    )
    columns: list[DesiredColumn] = Field(
        min_length=1,
        max_length=1000,
        description="按 DDL 顺序排列的物理字段列表。",
    )
    primary_key: list[str] = Field(
        min_length=1,
        max_length=32,
        description="按 DDL 顺序排列的主键字段名称。",
    )
    schema_fingerprint: str = Field(
        min_length=64,
        max_length=64,
        description="已接受物理模式的结构指纹。",
    )
    metric_dependency_column_ids: list[str] = Field(
        default_factory=list,
        max_length=1000,
        description="指标定义实际依赖的字段标识。",
    )

    def desired_hash(self) -> str:
        """返回稳定期望状态哈希。"""
        # 全局 schema 指纹还包含其他表和注释，不能作为单表重建 generation。
        payload = self.model_dump_json(
            exclude_none=False,
            exclude={"schema_fingerprint", "metric_dependency_column_ids"},
        )
        return hashlib.sha256(payload.encode()).hexdigest()


class SyncRowEvent(ContractModel):
    """由 Binlog ROW 事件规范化后的单行变更。"""

    source: str = Field(description="命名数据源配置键。")
    source_schema: str = Field(description="源 MySQL 数据库名称。")
    source_table: str = Field(description="源 MySQL 表名称。")
    coordinate: BinlogCoordinate = Field(description="该行变更的 Binlog 位点。")
    operation: RowOperation = Field(description="行级写入、更新或删除操作。")
    before: dict[str, EncodedValue] | None = Field(
        default=None,
        description="更新或删除前的规范化源行。",
    )
    after: dict[str, EncodedValue] | None = Field(
        default=None,
        description="写入或更新后的规范化源行。",
    )


class KeyConflict(ContractModel):
    """目标主键已被其他来源占用的冲突。"""

    target_table: str = Field(description="发生冲突的 DW 目标表。")
    primary_key_hash: str = Field(description="规范化目标主键文档哈希。")
    owner_source: str = Field(description="首次成功写入该主键的数据源。")
    contender_source: str = Field(description="尝试覆盖该主键的数据源。")


def primary_key_identity(
    desired: DesiredSyncTable,
    row: Mapping[str, object],
) -> tuple[str, str]:
    """按字段类型规范化目标主键并返回稳定文档及哈希。"""
    encoded: dict[str, EncodedValue] = {}
    columns = {column.name: column for column in desired.columns}
    for name in desired.primary_key:
        value = row[name]
        data_type = columns[name].data_type.strip().upper()
        if data_type.startswith("BIT"):
            if isinstance(value, bytes):
                value = int.from_bytes(value, byteorder="big", signed=False)
            elif not isinstance(value, int):
                raise TypeError("MySQL BIT 主键必须解码为 bytes 或 int")
        elif data_type.startswith("SET"):
            if isinstance(value, set):
                value = ",".join(sorted(value))
            elif isinstance(value, str):
                value = ",".join(sorted(filter(None, value.split(","))))
            else:
                raise TypeError("MySQL SET 主键必须解码为 set[str] 或 str")
        encoded[name] = encode_row_value(value)
    return canonical_primary_key(desired.primary_key, encoded)


def encode_row_value(value: object, *, json_value: bool = False) -> EncodedValue:
    """把 MySQL 行值编码为可逆且稳定的 JSON 值。"""
    if json_value:
        return {
            "$json": json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
        }
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError("MySQL DECIMAL 不支持非有限值")
        sign, digits, raw_exponent = value.as_tuple()
        exponent = cast(int, raw_exponent)
        digits_list = list(digits)
        while digits_list and digits_list[-1] == 0 and exponent < 0:
            digits_list.pop()
            exponent += 1
        if not digits_list:
            return {"$decimal": "0"}
        coefficient = "".join(str(digit) for digit in digits_list)
        if exponent >= 0:
            rendered = coefficient + ("0" * exponent)
        else:
            split = len(coefficient) + exponent
            rendered = (
                coefficient[:split] + "." + coefficient[split:]
                if split > 0
                else "0." + ("0" * -split) + coefficient
            )
        return {"$decimal": ("-" if sign else "") + rendered}
    if isinstance(value, datetime):
        return {"$datetime": value.isoformat(timespec="microseconds")}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, time):
        return {"$time": value.isoformat(timespec="microseconds")}
    if isinstance(value, timedelta):
        return {"$timedelta_microseconds": str(value // timedelta(microseconds=1))}
    if isinstance(value, bytes):
        return {"$binary": base64.b64encode(value).decode("ascii")}
    if isinstance(value, set):
        if not all(isinstance(item, str) for item in value):
            raise TypeError("MySQL SET 行值只能包含字符串")
        return {"$set": ",".join(sorted(value))}
    if isinstance(value, (dict, list)):
        return {
            "$json": json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        }
    raise TypeError(f"不支持的 MySQL 行值类型：{type(value).__name__}")


def decode_row_value(value: EncodedValue) -> object:
    """把规范化 JSON 值还原为 MySQL 驱动可写入的值。"""
    if not isinstance(value, dict):
        return value
    if "$decimal" in value:
        return Decimal(value["$decimal"])
    if "$datetime" in value:
        return datetime.fromisoformat(value["$datetime"])
    if "$date" in value:
        return date.fromisoformat(value["$date"])
    if "$time" in value:
        return time.fromisoformat(value["$time"])
    if "$timedelta_microseconds" in value:
        return timedelta(microseconds=int(value["$timedelta_microseconds"]))
    if "$binary" in value:
        return base64.b64decode(value["$binary"])
    if "$set" in value:
        # MySQL SET 绑定值使用逗号分隔文本；空集合对应空字符串。
        return value["$set"]
    if "$json" in value:
        # 动态 SQL 使用原生绑定参数；规范 JSON 文本可由 MySQL JSON 列直接接收。
        return value["$json"]
    raise ValueError("未知的数据同步行值编码")


def canonical_primary_key(
    primary_key: list[str],
    row: dict[str, EncodedValue],
) -> tuple[str, str]:
    """返回稳定主键文档及其碰撞检测哈希。"""
    document = json.dumps(
        {name: row[name] for name in primary_key},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return document, hashlib.sha256(document.encode()).hexdigest()


def build_desired_tables(
    schema: PhysicalSchema,
    metadata: SemanticMetadata,
    metrics: list[MetricMetadata],
    *,
    default_source_schema: str,
) -> list[DesiredSyncTable]:
    """从已接受的物理、语义和指标契约构建同步期望状态。"""
    semantic_table_ids = {table.table_id for table in metadata.tables}
    physical_column_ids = {
        column.id for table in schema.tables for column in table.columns
    }
    metric_dependencies = {
        column_id for metric in metrics for column_id in metric.relevant_column_ids
    }
    missing = metric_dependencies - physical_column_ids
    if missing:
        raise DataAgentError(
            "missing_metric_dependency",
            "persist_snapshot",
            "指标依赖了当前物理模式中不存在的字段",
            details={"columns": ",".join(sorted(missing))},
        )
    desired: list[DesiredSyncTable] = []
    task_identities: set[tuple[str, str]] = set()
    for table in schema.tables:
        if table.id not in semantic_table_ids:
            raise DataAgentError(
                "missing_table_semantics",
                "persist_snapshot",
                "同步表缺少已验证的语义表定义",
                details={"table": table.qualified_name},
            )
        primary_key = table.primary_key or [
            column.name
            for column in table.columns
            if column.structural_role == "primary_key"
        ]
        if not primary_key:
            raise DataAgentError(
                "missing_primary_key",
                "persist_snapshot",
                "同步表必须声明主键",
                details={"table": table.qualified_name},
            )
        columns_by_name = {column.name: column for column in table.columns}
        unsupported_cursor_columns = [
            name
            for name in primary_key
            if columns_by_name[name]
            .data_type.strip()
            .upper()
            .startswith(("ENUM", "SET"))
        ]
        if unsupported_cursor_columns:
            raise DataAgentError(
                "unsupported_backfill_primary_key",
                "persist_snapshot",
                "同步表主键不能使用 ENUM 或 SET 类型",
                details={"columns": ",".join(unsupported_cursor_columns)},
            )
        column_ids = {column.id for column in table.columns}
        identity = (schema.source, table.name)
        if identity in task_identities:
            raise DataAgentError(
                "ambiguous_sync_target",
                "persist_snapshot",
                "同一数据源不能把多个物理表同步到同一 DW 目标",
                details={"source": schema.source, "target_table": table.name},
            )
        task_identities.add(identity)
        desired.append(
            DesiredSyncTable(
                source=schema.source,
                source_schema=table.schema_name or default_source_schema,
                source_table=table.name,
                target_table=table.name,
                columns=[
                    DesiredColumn(
                        id=column.id,
                        name=column.name,
                        data_type=column.data_type,
                        nullable=(
                            False
                            if column.structural_role == "primary_key"
                            else column.nullable
                        ),
                    )
                    for column in table.columns
                ],
                primary_key=primary_key,
                schema_fingerprint=schema.schema_fingerprint,
                metric_dependency_column_ids=sorted(metric_dependencies & column_ids),
            )
        )
    return desired
