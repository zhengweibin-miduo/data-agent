"""当前 DDL 的精确长期记忆检索与重校验。"""

from dataclasses import dataclass, field

from ddl_metadata.validation import (
    finalize_and_validate_metrics,
    validate_metadata,
)
from errors import DataAgentError
from identifiers import scope_fingerprint
from infrastructure.mysql import MySQLDatabase
from memory.domain.payloads import memory_content_hash
from memory.mysql.repository import (
    MemoryRepository,
    StoredMemory,
)
from models.memory import (
    BuiltinMemoryCategory,
    MemoryContent,
    MetricDefinitionContent,
    SemanticDecisionContent,
)
from models.physical import PhysicalSchema
from models.semantic import (
    MetricAnswer,
    MetricMetadata,
    MetricQuestion,
    SemanticColumn,
    SemanticMetadata,
    SemanticTable,
)
from settings import app_config


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
    # 步骤一：用户确认只提高可信优先级；没有确认内容时才回退到其他活动记忆。
    preferred = [
        memory for memory in memories if memory.content.trust == "user_confirmed"
    ]
    choices = preferred or memories
    if not choices:
        return None
    # 步骤二：同作用域存在多条不同的用户确认内容时显式报冲突，禁止任选其一。
    canonical = {
        memory.content.model_dump_json(exclude_none=True) for memory in choices
    }
    if len(preferred) > 1 and len(canonical) > 1:
        raise DataAgentError(
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
        # 步骤一：为当前物理对象计算作用域指纹，限定精确兼容记忆的查询范围。
        fingerprints = {
            object_id: scope_fingerprint(schema, object_id)
            for object_id in (
                *[table.id for table in schema.tables],
                *[column.id for table in schema.tables for column in table.columns],
            )
        }
        # 步骤二：批量读取语义与指标候选，并用权威内容哈希剔除损坏或陈旧载荷。
        async with MySQLDatabase.session() as session:
            repository = MemoryRepository(session)
            grouped = await repository.find_compatible_scopes(
                schema.source,
                fingerprints,
                BuiltinMemoryCategory.DDL_SEMANTIC.value,
                app_config.memory.ddl_semantic_content_version,
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
                app_config.memory.ddl_semantic_content_version,
            )
            metric_memories = [
                memory
                for memory in metric_memories
                if memory_content_hash(memory.content) == memory.detail.content_hash
            ]
        # 步骤三：只有当前作用域指纹精确匹配的权威记忆才能直接复用。混合检索
        # 仅适合向分类器提供提示，不能补齐完整语义并绕过字段资格重新判定。
        # 步骤四：按作用域选择唯一可信内容并投影语义 capsule，冲突不允许静默覆盖。
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
        # 步骤五：指纹与 content hash 命中仅产生候选，当前 DDL AST 校验始终
        # 拥有最终裁决权。
        metadata = SemanticMetadata(tables=tables, columns=columns)
        semantic_issues = validate_metadata(schema, metadata)
        if any(issue.code != "missing_object" for issue in semantic_issues):
            return LoadedMemoryContext([], None, [], [], [])
        if semantic_issues:
            return LoadedMemoryContext(capsule, None, [], [], [])

        # 步骤六：仅在语义完整有效时合并指标证据，并重新执行确定性指标校验。
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
        # 步骤七：只返回本轮校验通过的指标记忆，供后续候选构建保持来源关系。
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
