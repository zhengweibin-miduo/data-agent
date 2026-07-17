"""长期记忆检索、管理、修正和载荷重建检查。"""

import asyncio
from uuid import uuid4

from sqlalchemy import select

from app.client.mysql_client_manager import MysqlClientManager
from app.client.redis_client_manager import RedisClientManager
from app.conf.app_config import app_config
from app.model.ddl_metadata import (
    DdlJobRequest,
    MemoryKind,
    MemoryPatchRequest,
    MemoryRowStatus,
    MetricAnswer,
    MetricDefinitionContent,
    SemanticDecisionContent,
    UserAnswerContent,
)
from app.repository.ddl_metadata.memory import MemoryRepository
from app.repository.ddl_metadata.schema import table_info
from app.service.ddl_metadata.parser import parse_ddl
from app.service.ddl_metadata.errors import DdlMetadataError
from app.service.ddl_metadata.identifiers import scope_fingerprint
from app.service.ddl_metadata.job_store import JobStore
from app.service.ddl_metadata.memory_context import MemoryContextService
from app.service.ddl_metadata.memory_management import MemoryManagementService
from app.service.ddl_metadata.memory import (
    SnapshotService,
    build_accepted_memories,
)
from app_test.repository.ddl_metadata.fixtures import (
    cleanup_schema,
    ensure_schema,
    metric_bundle,
    semantic_for,
)


async def _cleanup_job(store: JobStore, job_id: str, source: str) -> None:
    """清理当前测试创建的活动任务。"""
    redis = RedisClientManager.get_client()
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
    await SnapshotService().persist(
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
    async with MysqlClientManager.session() as session:
        await MemoryRepository(session).upsert_candidates(
            build_accepted_memories(
                schema,
                metadata,
                questions,
                [unrelated_answer],
                [],
            )
        )
    context = await MemoryContextService().load(schema)
    assert context.answers == answers
    redis = RedisClientManager.initialize()
    store = JobStore(redis)
    service = MemoryManagementService(store)
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
        assert len(page.items) >= 6
        metric_item = next(
            item
            for item in page.items
            if item.kind == MemoryKind.METRIC_DEFINITION
        )
        metric_detail = await service.get_detail(metric_item.uid)
        assert isinstance(metric_detail.content, MetricDefinitionContent)
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
            or item.scope_key in {
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
        assert expected_reference_uids <= reference_uids
        try:
            await service.correct(
                metric_item.uid,
                metric_detail.content.model_copy(
                    update={
                        "metric": metric_detail.content.metric.model_copy(
                            update={"name": "renamed_metric"}
                        )
                    }
                ),
            )
        except DdlMetadataError as error:
            assert error.code == "memory_scope_conflict"
        else:
            raise AssertionError("指标修正不能改变稳定 ID 输入")
        target_item = next(
            item
            for item in page.items
            if item.kind == MemoryKind.SEMANTIC_DECISION
            and item.scope_key == schema.tables[0].id
        )
        target = await service.get_detail(target_item.uid)
        assert isinstance(target.content, SemanticDecisionContent)

        pinned = await service.patch(
            target.uid,
            MemoryPatchRequest(pinned=True),
        )
        assert pinned.pinned
        pinned_again = await service.patch(
            target.uid,
            MemoryPatchRequest(pinned=True),
        )
        assert pinned_again.pinned

        replacement_content = target.content.model_copy(
            update={
                "table": target.content.table.model_copy(
                    update={"description": "user corrected description"}
                )
                if target.content.table is not None
                else None
            }
        )
        correction = await service.correct(target.uid, replacement_content)
        assert correction.requires_reprocess
        old = await service.get_detail(target.uid)
        replacement = await service.get_detail(correction.memory_uid)
        assert old.row_status == MemoryRowStatus.ARCHIVED
        assert replacement.row_status == MemoryRowStatus.NORMAL
        assert replacement.pinned
        assert replacement.content.trust == "user_confirmed"
        assert replacement.payload.trust == "user_confirmed"
        assert any(
            relation.relation_type.value == "SUPERSEDES"
            and relation.related_memory_uid == target.uid
            for relation in replacement.relations
        )
        try:
            await service.correct(replacement.uid, replacement.content)
        except DdlMetadataError as error:
            assert error.code == "unchanged_correction"
        else:
            raise AssertionError("相同用户确认内容不能自我替代")

        async with MysqlClientManager.session() as session:
            description = await session.scalar(
                select(table_info.c.description).where(
                    table_info.c.id == schema.tables[0].id
                )
            )
        assert description == metadata.tables[0].description

        reprocessed = await MemoryContextService().load(schema)
        assert reprocessed.complete_semantic is not None
        await SnapshotService().persist(
            schema,
            reprocessed.complete_semantic,
            reprocessed.questions,
            reprocessed.answers,
            reprocessed.metrics,
            build_accepted_memories(
                schema,
                reprocessed.complete_semantic,
                reprocessed.questions,
                reprocessed.answers,
                reprocessed.metrics,
                reprocessed.reused_memory,
            ),
        )
        reapplied = await service.get_detail(correction.memory_uid)
        assert reapplied.row_status == MemoryRowStatus.NORMAL
        assert reapplied.content.trust == "user_confirmed"
        async with MysqlClientManager.session() as session:
            description = await session.scalar(
                select(table_info.c.description).where(
                    table_info.c.id == schema.tables[0].id
                )
            )
        assert description == "user corrected description"

        question_item = next(
            item
            for item in page.items
            if item.kind == MemoryKind.METRIC_QUESTION
        )
        question_detail = await service.get_detail(question_item.uid)
        try:
            await service.correct(
                question_detail.uid,
                question_detail.content,
            )
        except DdlMetadataError as error:
            assert error.code == "immutable_memory_kind"
        else:
            raise AssertionError("问题记忆必须不可修正")

        active = await store.submit(
            DdlJobRequest(
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
        except DdlMetadataError as error:
            assert error.code == "source_busy"
        else:
            raise AssertionError("活动来源任务必须阻止记忆变更")
        await _cleanup_job(store, active.job_id, source)
        active_job_id = None

        archived = await service.patch(
            correction.memory_uid,
            MemoryPatchRequest(row_status=MemoryRowStatus.ARCHIVED),
        )
        assert archived.row_status == MemoryRowStatus.ARCHIVED
        async with MysqlClientManager.session() as session:
            compatible = await MemoryRepository(session).find_compatible(
                source,
                schema.tables[0].id,
                scope_fingerprint(schema, schema.tables[0].id),
                MemoryKind.SEMANTIC_DECISION,
                app_config.memory.payload_version,
                app_config.memory.content_version,
            )
        assert compatible == []

    finally:
        if active_job_id is not None:
            await _cleanup_job(store, active_job_id, source)
        await cleanup_schema(schema)
        await MysqlClientManager.close()
        await RedisClientManager.close()


def test_memory_repository() -> None:
    """运行真实 MySQL/Redis 记忆仓储检查。"""
    asyncio.run(_test_memory_repository())


if __name__ == "__main__":
    test_memory_repository()
