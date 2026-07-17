"""长期记忆候选构建与快照事务同步。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.identifiers import memory_uid, scope_fingerprint
from data_agent.ddl_metadata.memory.payloads import build_memory_payload
from data_agent.ddl_metadata.models import (
    MemoryCandidate,
    MemoryContent,
    MemoryKind,
    MemoryRelationType,
    MetricAnswer,
    MetricDefinitionContent,
    MetricMetadata,
    MetricQuestion,
    MetricQuestionContent,
    PhysicalSchema,
    SemanticDecisionContent,
    SemanticMetadata,
    UserAnswerContent,
)
from data_agent.ddl_metadata.persistence.memory_repository import (
    MemoryRepository,
    StoredMemory,
)
from data_agent.ddl_metadata.persistence.metadata_repository import MetadataRepository
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.settings import app_config


def _candidate(
    source: str,
    scope_key: str,
    schema_fingerprint: str,
    content: MemoryContent,
    *,
    trust: Literal["model_validated", "user_confirmed"],
    pinned: bool = False,
    reference_uids: Iterable[str] = (),
    comment_uids: Iterable[str] = (),
    supersedes_uids: Iterable[str] = (),
) -> MemoryCandidate:
    """构建稳定 UID 的记忆候选。"""
    content = content.model_copy(update={"trust": trust})
    content_json = content.model_dump_json(exclude_none=True)
    uid = memory_uid(
        source,
        content.kind.value,
        scope_key,
        schema_fingerprint,
        content_json,
    )
    return MemoryCandidate(
        uid=uid,
        source=source,
        kind=MemoryKind(content.kind),
        scope_key=scope_key,
        schema_fingerprint=schema_fingerprint,
        pinned=pinned,
        content=content,
        payload=build_memory_payload(content),
        content_version=app_config.memory.content_version,
        reference_uids=list(reference_uids),
        comment_uids=list(comment_uids),
        supersedes_uids=list(supersedes_uids),
    )


def build_accepted_memories(
    schema: PhysicalSchema,
    metadata: SemanticMetadata,
    questions: list[MetricQuestion],
    answers: list[MetricAnswer],
    metrics: list[MetricMetadata],
    reused_memory: Iterable[MemoryContent] = (),
) -> list[MemoryCandidate]:
    """把最终接受结果转为规范记忆及关系。"""
    reused_trust: dict[
        str,
        Literal["model_validated", "user_confirmed"],
    ] = {
        content.model_dump_json(exclude={"trust"}, exclude_none=True): (content.trust)
        for content in reused_memory
    }

    def trust_for(
        content: MemoryContent,
    ) -> Literal["model_validated", "user_confirmed"]:
        return reused_trust.get(
            content.model_dump_json(exclude={"trust"}, exclude_none=True),
            "model_validated",
        )

    candidates: list[MemoryCandidate] = []
    decision_uids: dict[str, str] = {}
    for table in metadata.tables:
        content = SemanticDecisionContent(
            kind=MemoryKind.SEMANTIC_DECISION,
            table=table,
        )
        trust = trust_for(content)
        candidate = _candidate(
            schema.source,
            table.table_id,
            scope_fingerprint(schema, table.table_id),
            content,
            trust=trust,
            pinned=trust == "user_confirmed",
        )
        decision_uids[table.table_id] = candidate.uid
        candidates.append(candidate)
    for column in metadata.columns:
        content = SemanticDecisionContent(
            kind=MemoryKind.SEMANTIC_DECISION,
            column=column,
        )
        trust = trust_for(content)
        candidate = _candidate(
            schema.source,
            column.column_id,
            scope_fingerprint(schema, column.column_id),
            content,
            trust=trust,
            pinned=trust == "user_confirmed",
        )
        decision_uids[column.column_id] = candidate.uid
        candidates.append(candidate)

    question_uids: dict[str, str] = {}
    for question in questions:
        content = MetricQuestionContent(
            kind=MemoryKind.METRIC_QUESTION,
            question=question,
        )
        candidate = _candidate(
            schema.source,
            question.question_id,
            schema.schema_fingerprint,
            content,
            trust="model_validated",
            reference_uids=[
                decision_uids[object_id]
                for object_id in (
                    question.fact_table_id,
                    *question.column_ids,
                )
            ],
        )
        question_uids[question.question_id] = candidate.uid
        candidates.append(candidate)

    answer_uids: dict[str, str] = {}
    for answer in answers:
        content = UserAnswerContent(
            kind=MemoryKind.USER_ANSWER,
            answer=answer,
        )
        question_uid = question_uids[answer.question_id]
        candidate = _candidate(
            schema.source,
            answer.question_id,
            schema.schema_fingerprint,
            content,
            trust="user_confirmed",
            comment_uids=[question_uid],
        )
        answer_uids[answer.question_id] = candidate.uid
        candidates.append(candidate)

    for metric in metrics:
        content = MetricDefinitionContent(
            kind=MemoryKind.METRIC_DEFINITION,
            metric=metric,
        )
        references = {
            decision_uids[object_id]
            for object_id in (
                metric.fact_table_id,
                *metric.relevant_column_ids,
            )
        }
        references.update(
            uid
            for question_id in metric.answer_question_ids
            for uid in (
                question_uids[question_id],
                answer_uids[question_id],
            )
        )
        trust = trust_for(content)
        candidates.append(
            _candidate(
                schema.source,
                metric.id,
                schema.schema_fingerprint,
                content,
                trust=trust,
                pinned=trust == "user_confirmed",
                reference_uids=sorted(references),
            )
        )
    return candidates


class MetadataSnapshotService:
    """在一个 MySQL 事务内提交 Meta 与长期记忆。"""

    async def persist(
        self,
        schema: PhysicalSchema,
        metadata: SemanticMetadata,
        questions: list[MetricQuestion],
        answers: list[MetricAnswer],
        metrics: list[MetricMetadata],
        candidates: list[MemoryCandidate] | None = None,
    ) -> None:
        """提交最终通过校验的完整快照。"""
        accepted_memories = candidates or build_accepted_memories(
            schema,
            metadata,
            questions,
            answers,
            metrics,
        )
        async with MySQLDatabase.session() as session:
            await MetadataRepository(session).synchronize(schema, metadata, metrics)
            await MemoryRepository(session).upsert_candidates(accepted_memories)


async def upsert_correction(
    session: AsyncSession,
    target: StoredMemory,
    content: MemoryContent,
) -> MemoryCandidate:
    """在当前事务中追加用户确认的替代记忆。"""
    repository = MemoryRepository(session)
    candidate = _candidate(
        target.item.source,
        target.item.scope_key,
        target.item.schema_fingerprint,
        content,
        trust="user_confirmed",
        pinned=True,
        supersedes_uids=[target.item.uid],
    )
    if candidate.uid == target.item.uid:
        raise DDLMetadataError(
            "unchanged_correction",
            "memory_correction",
            "修正内容与当前用户确认记忆相同",
            http_status=409,
        )
    related = await repository.related_uids(
        {target.id},
        MemoryRelationType.REFERENCE,
    )
    candidate.reference_uids = sorted(related.get(target.id, set()))
    await repository.upsert_candidates([candidate])
    return candidate
