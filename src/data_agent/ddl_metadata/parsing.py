"""只解析 MySQL CREATE TABLE 的确定性 DDL 边界。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Literal

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import ParseError

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.identifiers import column_id, table_id
from data_agent.ddl_metadata.models.physical import (
    PhysicalColumn,
    PhysicalSchema,
    PhysicalTable,
)
from data_agent.settings import APISettings, app_config


def _literal_text(expression: exp.Expression | None) -> str | None:
    """提取 SQLGlot 字面量文本。"""
    if isinstance(expression, exp.Literal):
        return str(expression.this)
    return None


def _column_comment(column: exp.ColumnDef) -> str | None:
    """提取列 COMMENT。"""
    for constraint in column.constraints:
        kind = constraint.kind
        if isinstance(kind, exp.CommentColumnConstraint):
            return _literal_text(kind.this)
    return None


def _table_comment(create: exp.Create) -> str | None:
    """提取表 COMMENT。"""
    properties = create.args.get("properties")
    if isinstance(properties, exp.Properties):
        for prop in properties.expressions:
            if isinstance(prop, exp.SchemaCommentProperty):
                return _literal_text(prop.this)
    return None


def _constraint_column_names(
    schema: exp.Schema,
) -> tuple[set[str], set[str]]:
    """提取表级主键与外键列名。"""
    primary_keys: set[str] = set()
    foreign_keys: set[str] = set()
    for item in schema.expressions:
        expressions = item.expressions if isinstance(item, exp.Constraint) else [item]
        for constraint in expressions:
            if isinstance(constraint, exp.PrimaryKey):
                primary_keys.update(
                    identifier.name.casefold()
                    for identifier in constraint.expressions
                    if isinstance(identifier, exp.Identifier)
                )
            elif isinstance(constraint, exp.ForeignKey):
                foreign_keys.update(
                    identifier.name.casefold()
                    for identifier in constraint.expressions
                    if isinstance(identifier, exp.Identifier)
                )
    return primary_keys, foreign_keys


def _inline_role(
    column: exp.ColumnDef,
) -> Literal["primary_key", "foreign_key"] | None:
    """提取列级结构角色，主键优先。"""
    for constraint in column.constraints:
        if isinstance(constraint.kind, exp.PrimaryKeyColumnConstraint):
            return "primary_key"
    for constraint in column.constraints:
        if constraint.kind.find(exp.Reference) is not None:
            return "foreign_key"
    return None


def _parse_table(source: str, create: exp.Create) -> PhysicalTable:
    """把单个 CREATE TABLE AST 转为物理表。"""
    schema = create.this
    if not isinstance(schema, exp.Schema) or not isinstance(schema.this, exp.Table):
        raise DDLMetadataError(
            "invalid_create_table",
            "parse_ddl",
            "CREATE TABLE 缺少有效列定义",
        )
    table = schema.this
    name = table.name
    schema_name = table.db or None
    catalog_name = table.catalog or None
    qualified_name = ".".join(
        part for part in (catalog_name, schema_name, name) if part
    )
    identifier = table_id(source, qualified_name.casefold())
    table_primary, table_foreign = _constraint_column_names(schema)
    columns: list[PhysicalColumn] = []
    seen_columns: set[str] = set()
    for item in schema.expressions:
        if not isinstance(item, exp.ColumnDef):
            continue
        column_name = item.name
        normalized_name = column_name.casefold()
        if normalized_name in seen_columns:
            raise DDLMetadataError(
                "duplicate_column",
                "parse_ddl",
                f"表 {qualified_name} 存在重复列 {column_name}",
            )
        seen_columns.add(normalized_name)
        role = _inline_role(item)
        if normalized_name in table_primary:
            role = "primary_key"
        elif normalized_name in table_foreign and role != "primary_key":
            role = "foreign_key"
        data_type = item.args.get("kind")
        if not isinstance(data_type, exp.DataType):
            raise DDLMetadataError(
                "missing_column_type",
                "parse_ddl",
                f"列 {qualified_name}.{column_name} 缺少数据类型",
            )
        columns.append(
            PhysicalColumn(
                id=column_id(identifier, normalized_name),
                name=column_name,
                data_type=data_type.sql(dialect="mysql"),
                comment=_column_comment(item),
                structural_role=role,
            )
        )
    if not columns:
        raise DDLMetadataError(
            "empty_table",
            "parse_ddl",
            f"表 {qualified_name} 未定义列",
        )
    unknown_keys = (table_primary | table_foreign) - seen_columns
    if unknown_keys:
        raise DDLMetadataError(
            "unknown_constraint_column",
            "parse_ddl",
            "约束引用了未定义列",
            details={"columns": ",".join(sorted(unknown_keys))},
        )
    return PhysicalTable(
        id=identifier,
        schema_name=schema_name,
        name=name,
        qualified_name=qualified_name,
        comment=_table_comment(create),
        columns=columns,
    )


def _parse_ddl_sync(
    source: str,
    ddl: str,
    limits: APISettings = app_config.api,
) -> PhysicalSchema:
    """在线程中解析并规范化有界 MySQL CREATE TABLE DDL。"""
    encoded_size = len(ddl.encode("utf-8"))
    if encoded_size > limits.max_ddl_bytes:
        raise DDLMetadataError(
            "ddl_too_large",
            "parse_ddl",
            "DDL 超过配置的字节限制",
            details={
                "actual": str(encoded_size),
                "limit": str(limits.max_ddl_bytes),
            },
        )
    try:
        statements = sqlglot.parse(ddl, read="mysql")
    except ParseError as error:
        raise DDLMetadataError(
            "malformed_ddl",
            "parse_ddl",
            "DDL 语法无效",
        ) from error
    if not statements:
        raise DDLMetadataError("empty_ddl", "parse_ddl", "DDL 不能为空")

    creates: list[exp.Create] = []
    for statement in statements:
        if (
            not isinstance(statement, exp.Create)
            or statement.args.get("kind") != "TABLE"
        ):
            raise DDLMetadataError(
                "unsupported_statement",
                "parse_ddl",
                "仅支持 MySQL CREATE TABLE 语句",
            )
        creates.append(statement)
    if len(creates) > limits.max_tables:
        raise DDLMetadataError(
            "too_many_tables",
            "parse_ddl",
            "表数量超过配置限制",
            details={
                "actual": str(len(creates)),
                "limit": str(limits.max_tables),
            },
        )

    tables = [_parse_table(source, create) for create in creates]
    normalized_tables = [table.qualified_name.casefold() for table in tables]
    if len(set(normalized_tables)) != len(normalized_tables):
        raise DDLMetadataError(
            "duplicate_table",
            "parse_ddl",
            "DDL 存在重复表",
        )
    column_count = sum(len(table.columns) for table in tables)
    if column_count > limits.max_columns:
        raise DDLMetadataError(
            "too_many_columns",
            "parse_ddl",
            "列数量超过配置限制",
            details={
                "actual": str(column_count),
                "limit": str(limits.max_columns),
            },
        )

    canonical_ddl = ";\n".join(
        create.sql(dialect="mysql", pretty=True) for create in creates
    )
    ddl_hash = hashlib.sha256(canonical_ddl.encode()).hexdigest()
    physical_json = json.dumps(
        [
            {
                "qualified_name": table.qualified_name,
                "comment": table.comment,
                "columns": [
                    {
                        "name": column.name,
                        "type": column.data_type,
                        "comment": column.comment,
                        "role": column.structural_role,
                    }
                    for column in table.columns
                ],
            }
            for table in tables
        ],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    schema_fingerprint = hashlib.sha256(physical_json.encode()).hexdigest()
    return PhysicalSchema(
        source=source,
        canonical_ddl=canonical_ddl,
        ddl_hash=ddl_hash,
        schema_fingerprint=schema_fingerprint,
        tables=tables,
    )


async def parse_ddl(
    source: str,
    ddl: str,
    limits: APISettings = app_config.api,
) -> PhysicalSchema:
    """在线程边界外解析并规范化有界 MySQL CREATE TABLE DDL。"""
    return await asyncio.to_thread(_parse_ddl_sync, source, ddl, limits)
