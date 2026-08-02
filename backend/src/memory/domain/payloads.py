"""权威内容到有界检索投影的确定性转换。"""

import hashlib
import json

from models.memory import (
    MemoryContent,
    MemoryProjection,
    MetricDefinitionContent,
    SemanticDecisionContent,
    UserMemoryContent,
)

_MAX_MEMORY_TEXT_LENGTH = 2048


def canonical_content_json(content: MemoryContent) -> str:
    """生成稳定且不含空白差异的权威内容 JSON。"""
    return json.dumps(
        content.model_dump(mode="json", exclude_none=True),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def memory_content_hash(content: MemoryContent) -> str:
    """计算权威类型化内容的 SHA-256。"""
    return hashlib.sha256(canonical_content_json(content).encode()).hexdigest()


def memory_text_hash(memory_text: str) -> str:
    """计算检索文本的 SHA-256，使精确基线检索能走等值索引。

    `memory_text` 是 TEXT 列，直接做全等比较无法走索引，数据量增长后精确基线查询
    会退化为按 source 范围扫描并成为检索延迟主项。比较定长哈希则可用普通 B-Tree
    索引。哈希只用于加速等值查找，权威内容与去重仍以 `content_hash` 为准。

    Args:
        memory_text: `build_memory_text` 生成的规范检索文本。

    Returns:
        64 位十六进制 SHA-256。
    """
    return hashlib.sha256(memory_text.encode()).hexdigest()


def content_object_ids(content: MemoryContent) -> list[str]:
    """提取规范内容引用的物理或 Meta 对象标识。"""
    if isinstance(content, SemanticDecisionContent):
        if content.table is not None:
            return [content.table.table_id]
        if content.column is not None:
            return [content.column.column_id]
    elif isinstance(content, MetricDefinitionContent):
        return [
            content.metric.id,
            content.metric.fact_table_id,
            *content.metric.relevant_column_ids,
        ]
    elif isinstance(content, UserMemoryContent):
        return list(content.evidence_message_uids)
    return []


def build_memory_text(content: MemoryContent) -> str:
    """从已验证内容生成自包含、有界且稳定的检索文本。"""
    if isinstance(content, SemanticDecisionContent):
        decision = content.table or content.column
        if decision is None:
            raise ValueError("语义决策缺少对象")
        if content.table is not None:
            object_id = content.table.table_id
        elif content.column is not None:
            object_id = content.column.column_id
        else:
            raise ValueError("语义决策缺少对象")
        text = "；".join(
            (
                "类型：ddl.semantic",
                f"对象：{object_id}",
                f"角色：{decision.role.value}",
                f"描述：{decision.description}",
                f"别名：{'、'.join(sorted(decision.aliases))}",
            )
        )
    elif isinstance(content, MetricDefinitionContent):
        metric = content.metric
        text = (
            f"类型：ddl.metric；指标：{metric.name}；"
            f"定义：{metric.definition}；事实表：{metric.fact_table_id}；"
            f"相关列：{'、'.join(sorted(metric.relevant_column_ids))}；"
            f"别名：{'、'.join(sorted(metric.aliases))}"
        )
    elif isinstance(content, UserMemoryContent):
        text = f"类型：user.fact；用户确认事实：{content.value}"
    else:
        text = json.dumps(content.data, ensure_ascii=False, sort_keys=True)
    return text[:_MAX_MEMORY_TEXT_LENGTH]


def projection_object_ids(projection: MemoryProjection) -> list[str]:
    """返回已去重排序的投影对象标识。"""
    return sorted(set(projection.object_ids))
