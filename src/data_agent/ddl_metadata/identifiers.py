"""元数据稳定标识符。"""

import hashlib
import json

from data_agent.ddl_metadata.models import PhysicalSchema


def stable_id(kind: str, *parts: str) -> str:
    """按设计约定生成稳定的 64 位十六进制标识。"""
    value = "\0".join((kind, *parts))
    return hashlib.sha256(value.encode()).hexdigest()


def table_id(source: str, qualified_name: str) -> str:
    """生成表标识。"""
    return stable_id("table", source, qualified_name)


def column_id(table_identifier: str, name: str) -> str:
    """生成列标识。"""
    return stable_id("column", table_identifier, name)


def metric_id(source: str, fact_table_id: str, name: str) -> str:
    """生成指标标识。"""
    return stable_id("metric", source, fact_table_id, name.strip().casefold())


def memory_uid(
    source: str,
    kind: str,
    scope_key: str,
    schema_fingerprint: str,
    content_json: str,
) -> str:
    """生成规范内容稳定的记忆标识。"""
    content_hash = hashlib.sha256(content_json.encode()).hexdigest()
    return stable_id(
        "memory",
        source,
        kind,
        scope_key,
        schema_fingerprint,
        content_hash,
    )


def scope_fingerprint(schema: PhysicalSchema, scope_key: str) -> str:
    """为表/列生成局部指纹，其余作用域复用完整模式指纹。"""
    for table in schema.tables:
        if table.id == scope_key:
            value = table.model_dump(mode="json")
            break
        for column in table.columns:
            if column.id == scope_key:
                value = {
                    "table_id": table.id,
                    "column": column.model_dump(mode="json"),
                }
                break
        else:
            continue
        break
    else:
        return schema.schema_fingerprint
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(encoded.encode()).hexdigest()
