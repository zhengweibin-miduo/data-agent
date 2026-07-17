"""长期记忆派生载荷的构建与批量重建。"""

import json

from loguru import logger

from data_agent.ddl_metadata.models import (
    MEMORY_CONTENT_ADAPTER,
    MemoryContent,
    MemoryKind,
    MemoryPayload,
    PayloadRebuildResult,
)
from data_agent.ddl_metadata.persistence.memory_repository import MemoryRepository
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.settings import app_config


def content_object_ids(content: MemoryContent) -> list[str]:
    """提取规范内容引用的物理或 Meta 对象标识。"""
    if content.kind == MemoryKind.SEMANTIC_DECISION:
        if content.table is not None:
            return [content.table.table_id]
        if content.column is not None:
            return [content.column.column_id]
    elif content.kind == MemoryKind.METRIC_QUESTION:
        return [
            content.question.fact_table_id,
            *content.question.column_ids,
        ]
    elif content.kind == MemoryKind.METRIC_DEFINITION:
        return [
            content.metric.id,
            content.metric.fact_table_id,
            *content.metric.relevant_column_ids,
        ]
    return []


def build_memory_payload(content: MemoryContent) -> MemoryPayload:
    """从规范内容确定性重建有界检索载荷。"""
    tags: set[str] = {content.kind.value}
    if content.kind == MemoryKind.SEMANTIC_DECISION:
        decision = content.table or content.column
        if decision is None:
            raise ValueError("语义决策缺少对象")
        tags.update(decision.aliases)
        tags.add(decision.role.value)
    elif content.kind == MemoryKind.METRIC_DEFINITION:
        tags.add(content.metric.name)
        tags.update(content.metric.aliases)
    elif content.kind == MemoryKind.METRIC_QUESTION:
        tags.add(content.question.question_id)
    else:
        tags.add(content.answer.question_id)
    return MemoryPayload(
        version=app_config.memory.payload_version,
        trust=content.trust,
        object_ids=sorted(set(content_object_ids(content))),
        tags=sorted(tag[:128] for tag in tags if tag)[:100],
        model=(app_config.llm.model if content.trust == "model_validated" else None),
        prompt_version=app_config.llm.prompt_version,
        graph_version=app_config.llm.graph_version,
    )


class MemoryPayloadRebuilder:
    """从规范内容重建一个有界批次的派生载荷。"""

    async def rebuild(
        self,
        source: str | None = None,
        after_id: int = 0,
    ) -> PayloadRebuildResult:
        """逐行隔离失败，保留规范内容并报告计数。"""
        processed = succeeded = failed = 0
        async with MySQLDatabase.session() as session:
            repository = MemoryRepository(session)
            rows = await repository.rebuild_rows(
                app_config.memory.rebuild_batch_size,
                source,
                after_id,
            )
            for row in rows:
                processed += 1
                try:
                    content = MEMORY_CONTENT_ADAPTER.validate_python(
                        json.loads(row["content"])
                        if isinstance(row["content"], str)
                        else row["content"]
                    )
                    async with session.begin_nested():
                        await repository.update_payload(
                            int(row["id"]),
                            build_memory_payload(content),
                        )
                    succeeded += 1
                except Exception as error:
                    failed += 1
                    logger.bind(trace_id="-").warning(
                        "记忆载荷重建失败 uid={} error_type={}",
                        row["uid"],
                        type(error).__name__,
                    )
        return PayloadRebuildResult(
            processed=processed,
            succeeded=succeeded,
            failed=failed,
            next_after_id=(
                int(rows[-1]["id"])
                if len(rows) == app_config.memory.rebuild_batch_size
                else None
            ),
        )
