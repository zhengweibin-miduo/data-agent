"""基于已接受物理模式幂等演进 DW 表结构。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.dialects.mysql import dialect as mysql_dialect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlglot import exp, parse

from data_agent.data_sync.models import DesiredColumn, DesiredSyncTable
from data_agent.errors import DataAgentError

_TYPE_PATTERN = re.compile(
    r"^(?P<base>[A-Z]+)"
    r"(?:\((?P<first>\d+)(?:\s*,\s*(?P<second>\d+))?\))?"
    r"(?P<unsigned>\s+UNSIGNED)?$"
)
_INTEGER_RANGES = {
    ("TINYINT", False): (-(2**7), 2**7 - 1),
    ("TINYINT", True): (0, 2**8 - 1),
    ("SMALLINT", False): (-(2**15), 2**15 - 1),
    ("SMALLINT", True): (0, 2**16 - 1),
    ("MEDIUMINT", False): (-(2**23), 2**23 - 1),
    ("MEDIUMINT", True): (0, 2**24 - 1),
    ("INT", False): (-(2**31), 2**31 - 1),
    ("INT", True): (0, 2**32 - 1),
    ("BIGINT", False): (-(2**63), 2**63 - 1),
    ("BIGINT", True): (0, 2**64 - 1),
}


@dataclass(frozen=True, slots=True)
class CurrentColumn:
    """DW 当前字段的最小结构投影。"""

    name: str
    data_type: str
    nullable: bool


@dataclass(frozen=True, slots=True)
class CurrentTable:
    """DW 当前表的字段和有序主键投影。"""

    columns: tuple[CurrentColumn, ...]
    primary_key: tuple[str, ...]


class DWSchemaSynchronizer:
    """检查并执行允许的 DW 加法式结构变更。"""

    def __init__(self, session: AsyncSession, *, database: str) -> None:
        """保存调用方 Session 和已验证的 DW 数据库名。"""
        self._session = session
        self._database = database

    async def synchronize(self, desired: DesiredSyncTable) -> None:
        """重查当前结构并执行创建、加列或安全扩宽。"""
        # 步骤一：从 information_schema 读取当前权威结构。
        current = await self.inspect(desired.target_table)
        # 步骤二：只执行确定性的加法式 DDL；MySQL 可在每条语句后自动提交。
        for statement in plan_schema_changes(
            database=self._database,
            desired=desired,
            current=current,
        ):
            await self._session.execute(text(statement))
        # 步骤三：重新读取并要求最终结构完全满足同一期望状态。
        remaining = plan_schema_changes(
            database=self._database,
            desired=desired,
            current=await self.inspect(desired.target_table),
        )
        if remaining:
            _raise_conflict(desired.target_table, "DDL 执行后目标结构仍未收敛")

    async def inspect(self, table_name: str) -> CurrentTable | None:
        """读取一张 DW 表的字段与主键结构。"""
        columns_result = await self._session.execute(
            text(
                """
                SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
                FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = :database AND TABLE_NAME = :table_name
                ORDER BY ORDINAL_POSITION
                """
            ),
            {"database": self._database, "table_name": table_name},
        )
        column_rows = columns_result.mappings().all()
        if not column_rows:
            return None
        primary_result = await self._session.execute(
            text(
                """
                SELECT COLUMN_NAME
                FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = :database
                  AND TABLE_NAME = :table_name
                  AND INDEX_NAME = 'PRIMARY'
                ORDER BY SEQ_IN_INDEX
                """
            ),
            {"database": self._database, "table_name": table_name},
        )
        return CurrentTable(
            columns=tuple(
                CurrentColumn(
                    name=str(row["COLUMN_NAME"]),
                    data_type=str(row["COLUMN_TYPE"]),
                    nullable=str(row["IS_NULLABLE"]) == "YES",
                )
                for row in column_rows
            ),
            primary_key=tuple(str(row[0]) for row in primary_result),
        )


def plan_schema_changes(
    *,
    database: str,
    desired: DesiredSyncTable,
    current: CurrentTable | None,
) -> list[str]:
    """返回使当前 DW 结构收敛到期望状态的最小 DDL 列表。"""
    _validate_metric_dependencies(desired)
    quote = mysql_dialect().identifier_preparer.quote
    qualified_table = f"{quote(database)}.{quote(desired.target_table)}"
    if current is None:
        columns = ", ".join(
            _column_definition(column, quote=quote) for column in desired.columns
        )
        primary_key = ", ".join(quote(name) for name in desired.primary_key)
        return [
            f"CREATE TABLE {qualified_table} "
            f"({columns}, PRIMARY KEY ({primary_key})) ENGINE=InnoDB "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin"
        ]

    current_by_name = {column.name: column for column in current.columns}
    desired_by_name = {column.name: column for column in desired.columns}
    extra = sorted(current_by_name.keys() - desired_by_name.keys())
    if extra:
        _raise_conflict(
            desired.target_table,
            f"目标表存在待删除字段：{','.join(extra)}",
        )
    if current.primary_key != tuple(desired.primary_key):
        _raise_conflict(desired.target_table, "目标表主键与已接受 DDL 不一致")

    changes: list[str] = []
    for desired_column in desired.columns:
        current_column = current_by_name.get(desired_column.name)
        definition = _column_definition(desired_column, quote=quote)
        if current_column is None:
            changes.append(f"ALTER TABLE {qualified_table} ADD COLUMN {definition}")
            continue
        if current_column.nullable != desired_column.nullable:
            _raise_conflict(
                desired.target_table,
                f"字段 {desired_column.name} 的可空性变化不允许自动执行",
            )
        desired_type = _canonical_type(desired_column.data_type)
        current_type = _normalize_type(current_column.data_type)
        if current_type == desired_type:
            continue
        if not is_safe_widening(current_type, desired_type):
            _raise_conflict(
                desired.target_table,
                f"字段 {desired_column.name} 的类型变化不安全",
            )
        changes.append(f"ALTER TABLE {qualified_table} MODIFY COLUMN {definition}")
    return changes


def is_safe_widening(current_type: str, desired_type: str) -> bool:
    """判断 MySQL 字段类型变化是否属于保守安全扩宽。"""
    current = _type_parts(current_type)
    desired = _type_parts(desired_type)
    if current is None or desired is None:
        return False
    current_base, current_first, current_second, current_unsigned = current
    desired_base, desired_first, desired_second, desired_unsigned = desired
    if current_base == "INTEGER":
        current_base = "INT"
    if desired_base == "INTEGER":
        desired_base = "INT"
    current_range = _INTEGER_RANGES.get((current_base, current_unsigned))
    desired_range = _INTEGER_RANGES.get((desired_base, desired_unsigned))
    if current_range is not None and desired_range is not None:
        return (
            desired_range[0] <= current_range[0]
            and desired_range[1] >= current_range[1]
        )
    if current_base == desired_base and current_base in {"VARCHAR", "VARBINARY"}:
        return (
            current_first is not None
            and desired_first is not None
            and desired_first >= current_first
        )
    if current_base == desired_base == "DECIMAL":
        if (
            current_first is None
            or current_second is None
            or desired_first is None
            or desired_second is None
        ):
            return False
        return (
            desired_second >= current_second
            and desired_first - desired_second >= current_first - current_second
        )
    return False


def _column_definition(
    column: DesiredColumn,
    *,
    quote: object,
) -> str:
    """生成已引用标识符的单个 MySQL 字段定义。"""
    quote_identifier = quote
    if not callable(quote_identifier):
        raise TypeError("MySQL 标识符引用器不可调用")
    nullable = " NULL" if column.nullable else " NOT NULL"
    return (
        f"{quote_identifier(column.name)} {_canonical_type(column.data_type)}{nullable}"
    )


def _canonical_type(data_type: str) -> str:
    """经 SQLGlot 重新解析一条字段类型并返回 MySQL 规范文本。"""
    expressions = parse(f"CREATE TABLE `_` (`_` {data_type})", read="mysql")
    if len(expressions) != 1 or not isinstance(expressions[0], exp.Create):
        raise ValueError("无效的 MySQL 字段类型")
    schema = expressions[0].this
    if not isinstance(schema, exp.Schema) or len(schema.expressions) != 1:
        raise ValueError("无效的 MySQL 字段类型")
    column = schema.expressions[0]
    if not isinstance(column, exp.ColumnDef):
        raise ValueError("无效的 MySQL 字段类型")
    kind = column.args.get("kind")
    if not isinstance(kind, exp.DataType):
        raise ValueError("无效的 MySQL 字段类型")
    return _normalize_type(kind.sql(dialect="mysql"))


def _normalize_type(data_type: str) -> str:
    """规范化 information_schema 与 SQLGlot 的类型文本。"""
    normalized = re.sub(r"\s+", " ", data_type.strip().upper())
    if normalized in {"BOOL", "BOOLEAN", "TINYINT(1)"}:
        return "BOOLEAN"
    return normalized


def _type_parts(data_type: str) -> tuple[str, int | None, int | None, bool] | None:
    """解析安全扩宽矩阵需要的有限 MySQL 类型参数。"""
    match = _TYPE_PATTERN.fullmatch(_normalize_type(data_type))
    if match is None:
        return None
    return (
        match.group("base"),
        int(match.group("first")) if match.group("first") else None,
        int(match.group("second")) if match.group("second") else None,
        match.group("unsigned") is not None,
    )


def _validate_metric_dependencies(desired: DesiredSyncTable) -> None:
    """确认指标依赖字段仍属于目标结构。"""
    column_ids = {column.id for column in desired.columns}
    missing = sorted(set(desired.metric_dependency_column_ids) - column_ids)
    if missing:
        _raise_conflict(
            desired.target_table,
            f"指标依赖字段不存在：{','.join(missing)}",
        )


def _raise_conflict(table_name: str, reason: str) -> None:
    """抛出可安全投影的非重试结构冲突。"""
    raise DataAgentError(
        "dw_schema_conflict",
        "schema_sync",
        reason,
        details={"table": table_name, "reason": reason},
    )
