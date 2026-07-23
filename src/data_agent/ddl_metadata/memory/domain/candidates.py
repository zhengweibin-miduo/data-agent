"""已接受内容到权威记忆候选的确定性构建。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal, cast

from data_agent.ddl_metadata.identifiers import memory_uid, scope_fingerprint
from data_agent.ddl_metadata.memory.domain.payloads import (
    build_memory_text,
    canonical_content_json,
    memory_content_hash,
)
from data_agent.ddl_metadata.memory.domain.policies import category_policy
from data_agent.ddl_metadata.models.memory import (
    BuiltinMemoryCategory,
    MemoryCandidate,
    MemoryContent,
    MemoryTrust,
    MetricDefinitionContent,
    SemanticDecisionContent,
)
from data_agent.ddl_metadata.models.physical import PhysicalSchema
from data_agent.ddl_metadata.models.semantic import (
    MetricAnswer,
    MetricMetadata,
    MetricQuestion,
    SemanticMetadata,
)
from data_agent.settings import app_config


def _candidate(
    source: str,
    category: str,
    memory_key: str,
    schema_fingerprint: str,
    content: MemoryContent,
    *,
    job_id: str,
    trust: Literal["model_validated", "user_confirmed"],
    derived_from_uids: Iterable[str] = (),
    related_uids: Iterable[str] = (),
    supersedes_uids: Iterable[str] = (),
) -> MemoryCandidate:
    """构建内容寻址且可重放的权威记忆候选。"""
    content = content.model_copy(update={"trust": trust})
    content_json = canonical_content_json(content)
    policy = category_policy(category)
    return MemoryCandidate(
        uid=memory_uid(
            source,
            category,
            memory_key,
            schema_fingerprint,
            content_json,
        ),
        source=source,
        category=category,
        memory_key=memory_key,
        content_schema=policy.content_schema,
        schema_fingerprint=schema_fingerprint,
        memory_text=build_memory_text(content),
        content=content,
        content_hash=memory_content_hash(content),
        trust=MemoryTrust(trust),
        content_version=app_config.memory.content_version,
        projection_version=app_config.memory.projection_version,
        importance_score=policy.importance_score,
        lifecycle_policy=policy.lifecycle_policy,
        created_job_id=job_id,
        derived_from_uids=list(derived_from_uids),
        related_uids=list(related_uids),
        supersedes_uids=list(supersedes_uids),
    )


def build_accepted_memories(
    schema: PhysicalSchema,
    metadata: SemanticMetadata,
    questions: list[MetricQuestion],
    answers: list[MetricAnswer],
    metrics: list[MetricMetadata],
    reused_memory: Iterable[MemoryContent] = (),
    *,
    job_id: str = "workflow",
) -> list[MemoryCandidate]:
    """把最终接受的类型化结果确定性转换为 ADD-only 候选。"""
    reused_trust = {
        content.model_dump_json(exclude={"trust"}, exclude_none=True): content.trust
        for content in reused_memory
    }

    def trust_for(
        content: MemoryContent,
    ) -> Literal["model_validated", "user_confirmed"]:
        return cast(
            Literal["model_validated", "user_confirmed"],
            reused_trust.get(
                content.model_dump_json(exclude={"trust"}, exclude_none=True),
                "model_validated",
            ),
        )

    candidates: list[MemoryCandidate] = []
    decisions: dict[str, str] = {}
    for table in metadata.tables:
        content = SemanticDecisionContent(
            table=table,
        )
        candidate = _candidate(
            schema.source,
            BuiltinMemoryCategory.DDL_SEMANTIC.value,
            table.table_id,
            scope_fingerprint(schema, table.table_id),
            content,
            job_id=job_id,
            trust=trust_for(content),
        )
        decisions[table.table_id] = candidate.uid
        candidates.append(candidate)
    for column in metadata.columns:
        content = SemanticDecisionContent(
            column=column,
        )
        candidate = _candidate(
            schema.source,
            BuiltinMemoryCategory.DDL_SEMANTIC.value,
            column.column_id,
            scope_fingerprint(schema, column.column_id),
            content,
            job_id=job_id,
            trust=trust_for(content),
        )
        decisions[column.column_id] = candidate.uid
        candidates.append(candidate)

    questions_by_id = {question.question_id: question for question in questions}
    answers_by_id = {answer.question_id: answer for answer in answers}
    for metric in metrics:
        metric_questions = [
            questions_by_id[question_id]
            for question_id in metric.answer_question_ids
            if question_id in questions_by_id
        ]
        metric_answers = [
            answers_by_id[question_id]
            for question_id in metric.answer_question_ids
            if question_id in answers_by_id
        ]
        content = MetricDefinitionContent(
            metric=metric,
            questions=metric_questions,
            answers=metric_answers,
        )
        derived_from = {
            decisions[object_id]
            for object_id in (
                metric.fact_table_id,
                *metric.relevant_column_ids,
            )
        }
        candidates.append(
            _candidate(
                schema.source,
                BuiltinMemoryCategory.DDL_METRIC.value,
                metric.id,
                schema.schema_fingerprint,
                content,
                job_id=job_id,
                trust=trust_for(content),
                derived_from_uids=sorted(derived_from),
            )
        )
    return candidates
