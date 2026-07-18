"""长期记忆检索、管理、修正和载荷重建检查。"""

from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import select

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.identifiers import scope_fingerprint
from data_agent.ddl_metadata.jobs.store import DDLJobStore
from data_agent.ddl_metadata.memory.context import MemoryContextLoader
from data_agent.ddl_metadata.memory.service import MemoryService
from data_agent.ddl_metadata.memory.snapshots import (
    MetadataSnapshotService,
    build_accepted_memories,
)
from data_agent.ddl_metadata.models import (
    DDLJobRequest,
    MemoryKind,
    MemoryPatchRequest,
    MemoryRowStatus,
    MetricAnswer,
    MetricDefinitionContent,
    SemanticDecisionContent,
    SemanticMetadata,
    UserAnswerContent,
)
from data_agent.ddl_metadata.parsing import parse_ddl
from data_agent.ddl_metadata.persistence.memory_repository import MemoryRepository
from data_agent.ddl_metadata.persistence.tables import table_info
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.redis import RedisClient
from data_agent.settings import app_config
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)
from tests.helpers.factories import (
    cleanup_schema,
    ensure_schema,
    metric_bundle,
    semantic_for,
)


async def _cleanup_job(store: DDLJobStore, job_id: str, source: str) -> None:
    """清理当前测试创建的活动任务。"""
    redis = RedisClient.get_client()
    await redis.execute_command("DEL", store._job_key(job_id))
    await redis.execute_command("DEL", store._source_key(source))
    await redis.execute_command("ZREM", store.cleanup_key, job_id)
    await redis.execute_command(
        "ZREM",
        store.dispatch_key,
        f"{job_id}:0",
    )


async def _test_memory_repository() -> None:
    """验证精确复用、优先级、浏览器管理和失败隔离。"""
    await ensure_schema()
    source = f"memory_{uuid4().hex}"
    schema = parse_ddl(
        source,
        """
        CREATE TABLE fact_memory (
            id BIGINT PRIMARY KEY,
            amount DECIMAL(10,2)
        )
        """,
    )
    metadata = semantic_for(schema, fact=True)
    questions, answers, metrics = metric_bundle(schema)
    await MetadataSnapshotService().persist(
        schema,
        metadata,
        questions,
        answers,
        metrics,
    )
    unrelated_answer = MetricAnswer(
        question_id=questions[0].question_id,
        answer="An older audit answer that is not referenced",
    )
    async with MySQLDatabase.session() as session:
        await MemoryRepository(session).upsert_candidates(
            build_accepted_memories(
                schema,
                metadata,
                questions,
                [unrelated_answer],
                [],
            )
        )
    context = await MemoryContextLoader().load(schema)
    check_equal("_test_memory_repository 检查点 1", context.answers, answers)
    redis = RedisClient.initialize()
    store = DDLJobStore(redis)
    service = MemoryService(store)
    active_job_id: str | None = None
    try:
        page = await service.list_page(
            source,
            kind=None,
            row_status=MemoryRowStatus.NORMAL,
            pinned=None,
            limit=100,
            cursor=None,
        )
        check_condition(
            "_test_memory_repository 检查点 2",
            len(page.items) >= 6,
            expected="原断言条件成立",
        )
        metric_item = next(
            item for item in page.items if item.kind == MemoryKind.METRIC_DEFINITION
        )
        metric_detail = await service.get_detail(metric_item.uid)
        check_condition(
            "_test_memory_repository 检查点 3",
            isinstance(metric_detail.content, MetricDefinitionContent),
            expected="原断言条件成立",
        )
        metric_content = cast(MetricDefinitionContent, metric_detail.content)
        reference_uids = {
            relation.related_memory_uid
            for relation in metric_detail.relations
            if relation.memory_uid == metric_item.uid
            and relation.relation_type.value == "REFERENCE"
        }
        expected_reference_uids = {
            item.uid
            for item in page.items
            if item.kind == MemoryKind.METRIC_QUESTION
            or item.scope_key
            in {
                schema.tables[0].id,
                metrics[0].relevant_column_ids[0],
            }
        }
        for item in page.items:
            if item.kind != MemoryKind.USER_ANSWER:
                continue
            answer_detail = await service.get_detail(item.uid)
            if (
                isinstance(answer_detail.content, UserAnswerContent)
                and answer_detail.content.answer == answers[0]
            ):
                expected_reference_uids.add(item.uid)
        check_condition(
            "_test_memory_repository 检查点 4",
            expected_reference_uids <= reference_uids,
            expected="原断言条件成立",
        )
        try:
            await service.correct(
                metric_item.uid,
                metric_content.model_copy(
                    update={
                        "metric": metric_content.metric.model_copy(
                            update={"name": "renamed_metric"}
                        )
                    }
                ),
            )
        except DDLMetadataError as error:
            check_exception(
                "_test_memory_repository 捕获预期异常", error, DDLMetadataError
            )
            check_equal(
                "_test_memory_repository 检查点 5",
                error.code,
                "memory_scope_conflict",
            )
        else:
            fail_check(
                "_test_memory_repository",
                actual="未抛出预期异常",
                expected="指标修正不能改变稳定 ID 输入",
            )
        target_item = next(
            item
            for item in page.items
            if item.kind == MemoryKind.SEMANTIC_DECISION
            and item.scope_key == schema.tables[0].id
        )
        target = await service.get_detail(target_item.uid)
        check_condition(
            "_test_memory_repository 检查点 6",
            isinstance(target.content, SemanticDecisionContent),
            expected="原断言条件成立",
        )
        target_content = cast(SemanticDecisionContent, target.content)

        pinned = await service.patch(
            target.uid,
            MemoryPatchRequest(pinned=True),
        )
        check_condition(
            "_test_memory_repository 检查点 7",
            pinned.pinned,
            expected="原断言条件成立",
        )
        pinned_again = await service.patch(
            target.uid,
            MemoryPatchRequest(pinned=True),
        )
        check_condition(
            "_test_memory_repository 检查点 8",
            pinned_again.pinned,
            expected="原断言条件成立",
        )

        replacement_content = target_content.model_copy(
            update={
                "table": target_content.table.model_copy(
                    update={"description": "user corrected description"}
                )
                if target_content.table is not None
                else None
            }
        )
        correction = await service.correct(target.uid, replacement_content)
        check_condition(
            "_test_memory_repository 检查点 9",
            correction.requires_reprocess,
            expected="原断言条件成立",
        )
        old = await service.get_detail(target.uid)
        replacement = await service.get_detail(correction.memory_uid)
        check_equal(
            "_test_memory_repository 检查点 10",
            old.row_status,
            MemoryRowStatus.ARCHIVED,
        )
        check_equal(
            "_test_memory_repository 检查点 11",
            replacement.row_status,
            MemoryRowStatus.NORMAL,
        )
        check_condition(
            "_test_memory_repository 检查点 12",
            replacement.pinned,
            expected="原断言条件成立",
        )
        check_equal(
            "_test_memory_repository 检查点 13",
            replacement.content.trust,
            "user_confirmed",
        )
        check_equal(
            "_test_memory_repository 检查点 14",
            replacement.payload.trust,
            "user_confirmed",
        )
        check_condition(
            "_test_memory_repository 检查点 15",
            any(
                relation.relation_type.value == "SUPERSEDES"
                and relation.related_memory_uid == target.uid
                for relation in replacement.relations
            ),
            expected="原断言条件成立",
        )
        try:
            await service.correct(replacement.uid, replacement.content)
        except DDLMetadataError as error:
            check_exception(
                "_test_memory_repository 捕获预期异常", error, DDLMetadataError
            )
            check_equal(
                "_test_memory_repository 检查点 16",
                error.code,
                "unchanged_correction",
            )
        else:
            fail_check(
                "_test_memory_repository",
                actual="未抛出预期异常",
                expected="相同用户确认内容不能自我替代",
            )

        async with MySQLDatabase.session() as session:
            description = await session.scalar(
                select(table_info.c.description).where(
                    table_info.c.id == schema.tables[0].id
                )
            )
        check_equal(
            "_test_memory_repository 检查点 17",
            description,
            metadata.tables[0].description,
        )

        reprocessed = await MemoryContextLoader().load(schema)
        check_condition(
            "_test_memory_repository 检查点 18",
            reprocessed.complete_semantic is not None,
            expected="原断言条件成立",
        )
        complete_semantic = cast(SemanticMetadata, reprocessed.complete_semantic)
        await MetadataSnapshotService().persist(
            schema,
            complete_semantic,
            reprocessed.questions,
            reprocessed.answers,
            reprocessed.metrics,
            build_accepted_memories(
                schema,
                complete_semantic,
                reprocessed.questions,
                reprocessed.answers,
                reprocessed.metrics,
                reprocessed.reused_memory,
            ),
        )
        reapplied = await service.get_detail(correction.memory_uid)
        check_equal(
            "_test_memory_repository 检查点 19",
            reapplied.row_status,
            MemoryRowStatus.NORMAL,
        )
        check_equal(
            "_test_memory_repository 检查点 20",
            reapplied.content.trust,
            "user_confirmed",
        )
        async with MySQLDatabase.session() as session:
            description = await session.scalar(
                select(table_info.c.description).where(
                    table_info.c.id == schema.tables[0].id
                )
            )
        check_equal(
            "_test_memory_repository 检查点 21",
            description,
            "user corrected description",
        )

        question_item = next(
            item for item in page.items if item.kind == MemoryKind.METRIC_QUESTION
        )
        question_detail = await service.get_detail(question_item.uid)
        try:
            await service.correct(
                question_detail.uid,
                question_detail.content,
            )
        except DDLMetadataError as error:
            check_exception(
                "_test_memory_repository 捕获预期异常", error, DDLMetadataError
            )
            check_equal(
                "_test_memory_repository 检查点 22",
                error.code,
                "immutable_memory_kind",
            )
        else:
            fail_check(
                "_test_memory_repository",
                actual="未抛出预期异常",
                expected="问题记忆必须不可修正",
            )

        active = await store.submit(
            DDLJobRequest(
                source=source,
                ddl="CREATE TABLE fact_memory (id BIGINT PRIMARY KEY)",
            )
        )
        active_job_id = active.job_id
        try:
            await service.patch(
                correction.memory_uid,
                MemoryPatchRequest(pinned=False),
            )
        except DDLMetadataError as error:
            check_exception(
                "_test_memory_repository 捕获预期异常", error, DDLMetadataError
            )
            check_equal(
                "_test_memory_repository 检查点 23",
                error.code,
                "source_busy",
            )
        else:
            fail_check(
                "_test_memory_repository",
                actual="未抛出预期异常",
                expected="活动来源任务必须阻止记忆变更",
            )
        await _cleanup_job(store, active.job_id, source)
        active_job_id = None

        archived = await service.patch(
            correction.memory_uid,
            MemoryPatchRequest(row_status=MemoryRowStatus.ARCHIVED),
        )
        check_equal(
            "_test_memory_repository 检查点 24",
            archived.row_status,
            MemoryRowStatus.ARCHIVED,
        )
        async with MySQLDatabase.session() as session:
            compatible = await MemoryRepository(session).find_compatible(
                source,
                schema.tables[0].id,
                scope_fingerprint(schema, schema.tables[0].id),
                MemoryKind.SEMANTIC_DECISION,
                app_config.memory.payload_version,
                app_config.memory.content_version,
            )
        check_equal("_test_memory_repository 检查点 25", compatible, [])

    finally:
        if active_job_id is not None:
            await _cleanup_job(store, active_job_id, source)
        await cleanup_schema(schema)
        await MySQLDatabase.close()
        await RedisClient.close()


@pytest.mark.integration
async def test_memory_repository() -> None:
    """运行真实 MySQL/Redis 记忆仓储检查。"""
    await _test_memory_repository()
