"""只解析 MySQL CREATE TABLE 的确定性 DDL 边界。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Literal

import sqlglot
from sqlglot import expressions as exp
from sqlglot.errors import ParseError

from data_agent.errors import DataAgentError
from data_agent.identifiers import column_id, table_id
from data_agent.models.physical import (
    PhysicalColumn,
    PhysicalRelationship,
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
) -> tuple[list[str], set[str]]:
    """提取表级主键与外键列名。"""
    primary_keys: list[str] = []
    foreign_keys: set[str] = set()
    for item in schema.expressions:
        expressions = item.expressions if isinstance(item, exp.Constraint) else [item]
        for constraint in expressions:
            if isinstance(constraint, exp.PrimaryKey):
                primary_keys.extend(
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


def _column_nullable(column: exp.ColumnDef, *, primary_key: bool) -> bool:
    """根据 AST 约束确定列是否允许空值。"""
    if primary_key:
        return False
    return not any(
        isinstance(constraint.kind, exp.NotNullColumnConstraint)
        for constraint in column.constraints
    )


def _parse_table(source: str, create: exp.Create) -> PhysicalTable:
    """把单个 CREATE TABLE AST 转为物理表。"""
    # 步骤一：先确定完整表身份并生成稳定 ID，同时汇总表级约束；后续列解析才能把
    # 列级与表级声明合并成唯一结构角色。
    schema = create.this
    if not isinstance(schema, exp.Schema) or not isinstance(schema.this, exp.Table):
        raise DataAgentError(
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
    table_primary_set = set(table_primary)
    # 步骤二：逐列执行名称去重、角色优先级和类型完整性校验，再投影为物理列模型。
    columns: list[PhysicalColumn] = []
    seen_columns: set[str] = set()
    for item in schema.expressions:
        if not isinstance(item, exp.ColumnDef):
            continue
        column_name = item.name
        normalized_name = column_name.casefold()
        if normalized_name in seen_columns:
            raise DataAgentError(
                "duplicate_column",
                "parse_ddl",
                f"表 {qualified_name} 存在重复列 {column_name}",
            )
        seen_columns.add(normalized_name)
        role = _inline_role(item)
        if normalized_name in table_primary_set:
            role = "primary_key"
        elif normalized_name in table_foreign and role != "primary_key":
            role = "foreign_key"
        data_type = item.args.get("kind")
        if not isinstance(data_type, exp.DataType):
            raise DataAgentError(
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
                nullable=_column_nullable(item, primary_key=role == "primary_key"),
                structural_role=role,
            )
        )
    # 步骤三：列集合完成后再校验表级约束引用，避免接受指向不存在列的物理结构。
    if not columns:
        raise DataAgentError(
            "empty_table",
            "parse_ddl",
            f"表 {qualified_name} 未定义列",
        )
    unknown_keys = (table_primary_set | table_foreign) - seen_columns
    if unknown_keys:
        raise DataAgentError(
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
        primary_key=(
            [
                column.name
                for name in table_primary
                for column in columns
                if column.name.casefold() == name
            ]
            or [
                column.name
                for column in columns
                if column.structural_role == "primary_key"
            ]
        ),
    )


def _relationships(
    creates: list[exp.Create],
    tables: list[PhysicalTable],
) -> list[PhysicalRelationship]:
    """把表级与列级外键约束规范化为字段引用边。"""
    relationships: list[PhysicalRelationship] = []
    for create, table in zip(creates, tables, strict=True):
        schema = create.this
        if not isinstance(schema, exp.Schema):
            continue
        columns = {column.name.casefold(): column for column in table.columns}

        def append_reference(
            source_name: str,
            reference: exp.Reference,
        ) -> None:
            """追加单字段外键引用。"""
            target = reference.this
            if not isinstance(target, exp.Schema) or not isinstance(
                target.this, exp.Table
            ):
                return
            target_columns = [
                identifier.name
                for identifier in target.expressions
                if isinstance(identifier, exp.Identifier)
            ]
            if len(target_columns) != 1:
                return
            relationships.append(
                PhysicalRelationship(
                    source_table_id=table.id,
                    source_column_id=columns[source_name.casefold()].id,
                    target_table=target.this.sql(dialect="mysql"),
                    target_column=target_columns[0],
                )
            )

        for foreign_key in schema.find_all(exp.ForeignKey):
            reference = foreign_key.args.get("reference")
            target = reference.this if isinstance(reference, exp.Reference) else None
            if not isinstance(target, exp.Schema) or not isinstance(
                target.this, exp.Table
            ):
                continue
            source_names = [
                identifier.name
                for identifier in foreign_key.expressions
                if isinstance(identifier, exp.Identifier)
            ]
            target_names = [
                identifier.name
                for identifier in target.expressions
                if isinstance(identifier, exp.Identifier)
            ]
            for source_name, target_name in zip(
                source_names, target_names, strict=True
            ):
                relationships.append(
                    PhysicalRelationship(
                        source_table_id=table.id,
                        source_column_id=columns[source_name.casefold()].id,
                        target_table=target.this.sql(dialect="mysql"),
                        target_column=target_name,
                    )
                )
        for column in schema.expressions:
            if not isinstance(column, exp.ColumnDef):
                continue
            for constraint in column.constraints:
                if isinstance(constraint.kind, exp.Reference):
                    append_reference(column.name, constraint.kind)
    return relationships


def _parse_ddl_sync(
    source: str,
    ddl: str,
    limits: APISettings = app_config.api,
) -> PhysicalSchema:
    """在线程中解析并规范化有界 MySQL CREATE TABLE DDL。"""
    # 步骤一：在进入 SQLGlot 前按 UTF-8 字节限制输入，并把语法错误收敛为安全业务错误。
    encoded_size = len(ddl.encode("utf-8"))
    if encoded_size > limits.max_ddl_bytes:
        raise DataAgentError(
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
        raise DataAgentError(
            "malformed_ddl",
            "parse_ddl",
            "DDL 语法无效",
        ) from error
    if not statements:
        raise DataAgentError("empty_ddl", "parse_ddl", "DDL 不能为空")

    # 步骤二：整批语句必须全部是 CREATE TABLE；混入其他 SQL 时拒绝整个快照，
    # 不对其中一部分表做部分解析或持久化。
    creates: list[exp.Create] = []
    for statement in statements:
        if (
            not isinstance(statement, exp.Create)
            or statement.args.get("kind") != "TABLE"
        ):
            raise DataAgentError(
                "unsupported_statement",
                "parse_ddl",
                "仅支持 MySQL CREATE TABLE 语句",
            )
        creates.append(statement)
    if len(creates) > limits.max_tables:
        raise DataAgentError(
            "too_many_tables",
            "parse_ddl",
            "表数量超过配置限制",
            details={
                "actual": str(len(creates)),
                "limit": str(limits.max_tables),
            },
        )

    # 步骤三：表级投影完成后统一检查跨语句重复表和全局列数上限。
    tables = [_parse_table(source, create) for create in creates]
    normalized_tables = [table.qualified_name.casefold() for table in tables]
    if len(set(normalized_tables)) != len(normalized_tables):
        raise DataAgentError(
            "duplicate_table",
            "parse_ddl",
            "DDL 存在重复表",
        )
    column_count = sum(len(table.columns) for table in tables)
    if column_count > limits.max_columns:
        raise DataAgentError(
            "too_many_columns",
            "parse_ddl",
            "列数量超过配置限制",
            details={
                "actual": str(column_count),
                "limit": str(limits.max_columns),
            },
        )
    missing_primary_key = next(
        (
            table.qualified_name
            for table in tables
            if not any(
                column.structural_role == "primary_key" for column in table.columns
            )
        ),
        None,
    )
    if missing_primary_key is not None:
        raise DataAgentError(
            "missing_primary_key",
            "parse_ddl",
            f"表 {missing_primary_key} 必须声明主键才能同步到 DW",
            details={"table": missing_primary_key},
        )

    # 步骤四：DDL 哈希描述规范化语句文本；Schema 指纹只描述物理表列投影。
    # 两者分离，使格式变化与真实结构变化拥有不同的稳定判定依据。
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
                        "nullable": column.nullable,
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
        relationships=_relationships(creates, tables),
    )


async def parse_ddl(
    source: str,
    ddl: str,
    limits: APISettings = app_config.api,
) -> PhysicalSchema:
    """在线程边界外解析并规范化有界 MySQL CREATE TABLE DDL。"""
    return await asyncio.to_thread(_parse_ddl_sync, source, ddl, limits)
