"""当前 DDL 的精确长期记忆检索与重校验。"""

from dataclasses import dataclass, field

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.identifiers import scope_fingerprint
from data_agent.ddl_metadata.models import (
    MemoryContent,
    MemoryKind,
    MemoryRelationType,
    MetricAnswer,
    MetricDefinitionContent,
    MetricMetadata,
    MetricQuestion,
    MetricQuestionContent,
    PhysicalSchema,
    SemanticColumn,
    SemanticDecisionContent,
    SemanticMetadata,
    SemanticTable,
    UserAnswerContent,
)
from data_agent.ddl_metadata.persistence.memory_repository import (
    MemoryRepository,
    StoredMemory,
)
from data_agent.ddl_metadata.validation import (
    finalize_and_validate_metrics,
    validate_metadata,
)
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.settings import app_config


@dataclass(frozen=True)
class LoadedMemoryContext:
    """当前图可安全复用的有界长期记忆。"""

    semantic_capsule: list[MemoryContent]
    complete_semantic: SemanticMetadata | None
    questions: list[MetricQuestion]
    answers: list[MetricAnswer]
    metrics: list[MetricMetadata]
    reused_memory: list[MemoryContent] = field(default_factory=list)


def _choose_memory(
    scope_key: str,
    memories: list[StoredMemory],
) -> StoredMemory | None:
    """应用 pinned user-confirmed 优先级并拒绝活动冲突。"""
    preferred = [
        memory
        for memory in memories
        if memory.item.pinned and memory.content.trust == "user_confirmed"
    ]
    choices = preferred or memories
    if not choices:
        return None
    canonical = {
        memory.content.model_dump_json(exclude_none=True) for memory in choices
    }
    if len(preferred) > 1 and len(canonical) > 1:
        raise DDLMetadataError(
            "memory_conflict",
            "load_and_validate_memory",
            "同一对象存在冲突的用户确认记忆",
            details={"scope_key": scope_key},
        )
    return choices[0]


class MemoryContextLoader:
    """加载兼容 capsule，并让当前 AST 再次拥有最终裁决权。"""

    async def load(
        self,
        schema: PhysicalSchema,
    ) -> LoadedMemoryContext:
        """批量读取语义记忆；完整有效时直接复用。"""
        fingerprints = {
            object_id: scope_fingerprint(schema, object_id)
            for object_id in (
                *[table.id for table in schema.tables],
                *[column.id for table in schema.tables for column in table.columns],
            )
        }
        async with MySQLDatabase.session() as session:
            repository = MemoryRepository(session)
            grouped = await repository.find_compatible_scopes(
                schema.source,
                fingerprints,
                MemoryKind.SEMANTIC_DECISION,
                app_config.memory.payload_version,
                app_config.memory.content_version,
            )
            metric_memories = await repository.find_active_by_fingerprint(
                schema.source,
                schema.schema_fingerprint,
                {
                    MemoryKind.METRIC_QUESTION,
                    MemoryKind.USER_ANSWER,
                    MemoryKind.METRIC_DEFINITION,
                },
                app_config.memory.payload_version,
                app_config.memory.content_version,
            )
            metric_reference_uids = await repository.related_uids(
                {
                    memory.id
                    for memory in metric_memories
                    if isinstance(
                        memory.content,
                        MetricDefinitionContent,
                    )
                },
                MemoryRelationType.REFERENCE,
            )
        capsule: list[MemoryContent] = []
        tables: list[SemanticTable] = []
        columns: list[SemanticColumn] = []
        for scope_key, memories in grouped.items():
            selected = _choose_memory(scope_key, memories)
            if selected is None:
                continue
            content = selected.content
            if not isinstance(content, SemanticDecisionContent):
                continue
            if set(selected.payload.object_ids) != {scope_key}:
                continue
            if content.table is not None and content.table.table_id == scope_key:
                tables.append(content.table)
            elif content.column is not None and content.column.column_id == scope_key:
                columns.append(content.column)
            else:
                continue
            capsule.append(content)
        metadata = SemanticMetadata(tables=tables, columns=columns)
        if validate_metadata(schema, metadata):
            return LoadedMemoryContext(capsule, None, [], [], [])

        referenced_uids = {
            uid for uids in metric_reference_uids.values() for uid in uids
        }
        questions = [
            memory.content.question
            for memory in metric_memories
            if isinstance(memory.content, MetricQuestionContent)
            and memory.item.uid in referenced_uids
        ]
        answers = [
            memory.content.answer
            for memory in metric_memories
            if isinstance(memory.content, UserAnswerContent)
            and memory.item.uid in referenced_uids
        ]
        metrics = [
            memory.content.metric
            for memory in metric_memories
            if isinstance(memory.content, MetricDefinitionContent)
        ]
        finalized, metric_issues = finalize_and_validate_metrics(
            schema.source,
            schema,
            metadata,
            questions,
            answers,
            metrics,
        )
        if metric_issues:
            questions = []
            answers = []
            finalized = []
        reused_metrics = {metric.id for metric in finalized}
        return LoadedMemoryContext(
            semantic_capsule=capsule,
            complete_semantic=metadata,
            questions=questions,
            answers=answers,
            metrics=finalized,
            reused_memory=[
                *capsule,
                *[
                    memory.content
                    for memory in metric_memories
                    if isinstance(
                        memory.content,
                        MetricDefinitionContent,
                    )
                    and memory.content.metric.id in reused_metrics
                ],
            ],
        )
