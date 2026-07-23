"""当前 DDL 的精确长期记忆检索与重校验。"""

from dataclasses import dataclass, field

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.identifiers import scope_fingerprint
from data_agent.ddl_metadata.memory.application.search import MemorySearchService
from data_agent.ddl_metadata.memory.domain.payloads import memory_content_hash
from data_agent.ddl_metadata.memory.mysql.repository import (
    MemoryRepository,
    StoredMemory,
)
from data_agent.ddl_metadata.models.memory import (
    BuiltinMemoryCategory,
    MemoryContent,
    MetricDefinitionContent,
    SemanticDecisionContent,
)
from data_agent.ddl_metadata.models.physical import PhysicalSchema
from data_agent.ddl_metadata.models.semantic import (
    MetricAnswer,
    MetricMetadata,
    MetricQuestion,
    SemanticColumn,
    SemanticMetadata,
    SemanticTable,
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
    """应用用户确认优先级并拒绝同作用域活动冲突。"""
    preferred = [
        memory for memory in memories if memory.content.trust == "user_confirmed"
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
            await repository.expire_fingerprint_bound(
                schema.source,
                set(fingerprints.values()),
                memory_keys=set(fingerprints),
            )
            grouped = await repository.find_compatible_scopes(
                schema.source,
                fingerprints,
                BuiltinMemoryCategory.DDL_SEMANTIC.value,
                app_config.memory.content_version,
            )
            grouped = {
                scope: [
                    memory
                    for memory in memories
                    if memory_content_hash(memory.content) == memory.detail.content_hash
                ]
                for scope, memories in grouped.items()
            }
            metric_memories = await repository.find_active_by_fingerprint(
                schema.source,
                schema.schema_fingerprint,
                {BuiltinMemoryCategory.DDL_METRIC.value},
                app_config.memory.content_version,
            )
            metric_memories = [
                memory
                for memory in metric_memories
                if memory_content_hash(memory.content) == memory.detail.content_hash
            ]
        allowed_object_ids = set(fingerprints)
        query = "；".join(
            value
            for table in schema.tables
            for value in (
                f"表 {table.qualified_name} {table.comment or ''}",
                *(
                    f"列 {table.qualified_name}.{column.name} "
                    f"{column.data_type} {column.comment or ''}"
                    for column in table.columns
                ),
            )
        )[:2000]
        hybrid = await MemorySearchService().search(
            query,
            schema.source,
            categories={BuiltinMemoryCategory.DDL_SEMANTIC.value},
            limit=app_config.memory.search_limit,
            allowed_object_ids=allowed_object_ids,
        )
        for hit in hybrid.items:
            scope = hit.memory.memory_key
            if scope not in grouped or grouped[scope]:
                continue
            grouped[scope] = [
                StoredMemory(
                    id=0,
                    detail=hit.memory,
                )
            ]
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
            if content.table is not None and content.table.table_id == scope_key:
                tables.append(content.table)
            elif content.column is not None and content.column.column_id == scope_key:
                columns.append(content.column)
            else:
                continue
            capsule.append(content)
        metadata = SemanticMetadata(tables=tables, columns=columns)
        semantic_issues = validate_metadata(schema, metadata)
        if any(issue.code != "missing_object" for issue in semantic_issues):
            return LoadedMemoryContext([], None, [], [], [])
        if semantic_issues:
            return LoadedMemoryContext(capsule, None, [], [], [])

        metric_contents = [
            memory.content
            for memory in metric_memories
            if isinstance(memory.content, MetricDefinitionContent)
        ]
        questions = list(
            {
                question.question_id: question
                for content in metric_contents
                for question in content.questions
            }.values()
        )
        answers = list(
            {
                answer.question_id: answer
                for content in metric_contents
                for answer in content.answers
            }.values()
        )
        metrics = [content.metric for content in metric_contents]
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
        reused_metric_content: list[MemoryContent] = []
        for memory in metric_memories:
            content = memory.content
            if (
                isinstance(content, MetricDefinitionContent)
                and content.metric.id in reused_metrics
            ):
                reused_metric_content.append(content)
        return LoadedMemoryContext(
            semantic_capsule=capsule,
            complete_semantic=metadata,
            questions=questions,
            answers=answers,
            metrics=finalized,
            reused_memory=[*capsule, *reused_metric_content],
        )
