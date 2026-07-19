"""权威内容到有界检索投影的确定性转换。"""

import hashlib
import json

from data_agent.ddl_metadata.models.memory import (
    MemoryContent,
    MemoryKind,
    MemoryProjection,
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


def content_object_ids(content: MemoryContent) -> list[str]:
    """提取规范内容引用的物理或 Meta 对象标识。"""
    if content.kind == MemoryKind.SEMANTIC_DECISION:
        if content.table is not None:
            return [content.table.table_id]
        if content.column is not None:
            return [content.column.column_id]
    elif content.kind == MemoryKind.METRIC_QUESTION:
        return [content.question.fact_table_id, *content.question.column_ids]
    elif content.kind == MemoryKind.METRIC_DEFINITION:
        return [
            content.metric.id,
            content.metric.fact_table_id,
            *content.metric.relevant_column_ids,
        ]
    return []


def build_memory_text(content: MemoryContent) -> str:
    """从已验证内容生成自包含、有界且稳定的检索文本。"""
    if content.kind == MemoryKind.SEMANTIC_DECISION:
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
                f"类型：{content.kind.value}",
                f"对象：{object_id}",
                f"角色：{decision.role.value}",
                f"描述：{decision.description}",
                f"别名：{'、'.join(sorted(decision.aliases))}",
            )
        )
    elif content.kind == MemoryKind.METRIC_QUESTION:
        question = content.question
        text = (
            f"类型：{content.kind.value}；问题标识：{question.question_id}；"
            f"问题：{question.prompt}；事实表：{question.fact_table_id}；"
            f"相关列：{'、'.join(sorted(question.column_ids))}"
        )
    elif content.kind == MemoryKind.USER_ANSWER:
        answer = content.answer
        text = (
            f"类型：{content.kind.value}；问题标识：{answer.question_id}；"
            f"用户确认回答：{answer.answer}"
        )
    else:
        metric = content.metric
        text = (
            f"类型：{content.kind.value}；指标：{metric.name}；"
            f"定义：{metric.definition}；事实表：{metric.fact_table_id}；"
            f"相关列：{'、'.join(sorted(metric.relevant_column_ids))}；"
            f"别名：{'、'.join(sorted(metric.aliases))}"
        )
    return text[:_MAX_MEMORY_TEXT_LENGTH]


def projection_object_ids(projection: MemoryProjection) -> list[str]:
    """返回已去重排序的投影对象标识。"""
    return sorted(set(projection.object_ids))
