"""Mem0 风格权威记忆仓储集成检查。"""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from data_agent.ddl_metadata.identifiers import memory_uid, scope_fingerprint
from data_agent.ddl_metadata.memory.domain.candidates import build_accepted_memories
from data_agent.ddl_metadata.memory.domain.payloads import (
    build_memory_text,
    canonical_content_json,
    memory_content_hash,
)
from data_agent.ddl_metadata.memory.mysql.repository import MemoryRepository
from data_agent.ddl_metadata.memory.mysql.tables import (
    agent_memory,
    memory_index_outbox,
)
from data_agent.ddl_metadata.models.memory import (
    MemoryEventType,
    MemoryIndexOperation,
    MemoryIndexTarget,
    MemoryStatus,
    MetricDefinitionContent,
    SemanticDecisionContent,
)
from data_agent.ddl_metadata.models.semantic import (
    MetricAnswer,
    MetricMetadata,
    MetricQuestion,
)
from data_agent.ddl_metadata.parsing import parse_ddl
from data_agent.infrastructure.mysql import MySQLDatabase
from tests.helpers.checks import check_equal
from tests.helpers.factories import cleanup_schema, ensure_schema, semantic_for


@pytest.mark.integration
async def test_memory_repository() -> None:
    """验证 ADD 幂等、历史、软删除和双目标 outbox。"""
    await ensure_schema()
    schema = await parse_ddl(
        f"memory_{uuid4().hex}",
        "CREATE TABLE dim_customer (id BIGINT PRIMARY KEY, name VARCHAR(64))",
    )
    candidates = build_accepted_memories(
        schema,
        semantic_for(schema, fact=False),
        [],
        [],
        [],
        job_id=uuid4().hex,
    )
    target_uid = candidates[0].uid
    try:
        async with MySQLDatabase.session() as session:
            repository = MemoryRepository(session)
            await repository.upsert_candidates(candidates)
            await repository.upsert_candidates(candidates)
            target = await repository.get_by_uid(target_uid)
            check_equal(
                "test_memory_repository 检查点 1",
                target is not None and target.detail.status,
                MemoryStatus.ACTIVE,
            )
            history = await repository.history(target_uid, offset=0, limit=20)
            check_equal(
                "test_memory_repository 检查点 2",
                [event.event_type for event in history.items] if history else [],
                [MemoryEventType.ADD, MemoryEventType.NOOP],
            )
            outbox_count = await session.scalar(
                select(func.count())
                .select_from(memory_index_outbox)
                .where(memory_index_outbox.c.memory_uid == target_uid)
            )
            check_equal(
                "test_memory_repository 检查点 3",
                outbox_count,
                len(MemoryIndexTarget),
            )
            if target is None:
                raise RuntimeError("测试记忆必须存在")
            await repository.soft_delete(target)
        async with MySQLDatabase.session() as session:
            repository = MemoryRepository(session)
            deleted = await repository.get_by_uid(target_uid)
            check_equal(
                "test_memory_repository 检查点 4",
                deleted is not None and deleted.detail.status,
                MemoryStatus.DELETED,
            )
            history = await repository.history(target_uid, offset=0, limit=20)
            check_equal(
                "test_memory_repository 检查点 5",
                [event.event_type for event in history.items] if history else [],
                [
                    MemoryEventType.ADD,
                    MemoryEventType.NOOP,
                    MemoryEventType.DELETE,
                ],
            )
            await repository.upsert_candidates(candidates)
        async with MySQLDatabase.session() as session:
            repository = MemoryRepository(session)
            replayed = await repository.get_by_uid(target_uid)
            check_equal(
                "test_memory_repository 检查点 6",
                replayed is not None and replayed.detail.status,
                MemoryStatus.DELETED,
            )
            outbox_operations = set(
                (
                    await session.scalars(
                        select(memory_index_outbox.c.operation).where(
                            memory_index_outbox.c.memory_uid == target_uid
                        )
                    )
                ).all()
            )
            check_equal(
                "test_memory_repository 检查点 7",
                outbox_operations,
                {MemoryIndexOperation.DELETE.value},
            )
    finally:
        await cleanup_schema(schema)
        await MySQLDatabase.close()


@pytest.mark.integration
async def test_superseded_content_can_become_active_again() -> None:
    """验证 A→B→A 时为重新生效的 A 创建新版本。"""
    await ensure_schema()
    schema = await parse_ddl(
        f"memory_reactivation_{uuid4().hex}",
        "CREATE TABLE dim_customer (id BIGINT PRIMARY KEY, name VARCHAR(64))",
    )
    candidates = build_accepted_memories(
        schema,
        semantic_for(schema, fact=False),
        [],
        [],
        [],
        job_id=uuid4().hex,
    )
    original_a = candidates[0]
    replayed_a = original_a.model_copy(deep=True)
    if not isinstance(original_a.content, SemanticDecisionContent):
        raise RuntimeError("测试候选必须是语义记忆")
    if original_a.content.table is None:
        raise RuntimeError("测试候选必须包含表语义")
    content_b = original_a.content.model_copy(
        update={
            "table": original_a.content.table.model_copy(
                update={"description": f"修正后的客户维表-{uuid4().hex}"}
            )
        }
    )
    content_b_json = canonical_content_json(content_b)
    candidate_b = original_a.model_copy(
        deep=True,
        update={
            "uid": memory_uid(
                original_a.source,
                original_a.category,
                original_a.memory_key,
                original_a.schema_fingerprint or "",
                content_b_json,
            ),
            "memory_text": build_memory_text(content_b),
            "content": content_b,
            "content_hash": memory_content_hash(content_b),
            "created_job_id": uuid4().hex,
        },
    )
    original_a_uid = original_a.uid
    candidate_b_uid = candidate_b.uid
    try:
        async with MySQLDatabase.session() as session:
            await MemoryRepository(session).upsert_candidates([original_a])
        async with MySQLDatabase.session() as session:
            await MemoryRepository(session).upsert_candidates([candidate_b])
        async with MySQLDatabase.session() as session:
            await MemoryRepository(session).upsert_candidates([replayed_a])
            reactivated_a_uid = replayed_a.uid
        async with MySQLDatabase.session() as session:
            rows = list(
                (
                    await session.execute(
                        select(
                            agent_memory.c.uid,
                            agent_memory.c.status,
                            agent_memory.c.record_version,
                            agent_memory.c.content_hash,
                        )
                        .where(
                            agent_memory.c.source == original_a.source,
                            agent_memory.c.category == original_a.category,
                            agent_memory.c.memory_key == original_a.memory_key,
                        )
                        .order_by(agent_memory.c.record_version)
                    )
                ).mappings()
            )
            check_equal(
                "test_superseded_content_can_become_active_again 检查点 1",
                [str(row["uid"]) for row in rows],
                [original_a_uid, candidate_b_uid, reactivated_a_uid],
            )
            check_equal(
                "test_superseded_content_can_become_active_again 检查点 2",
                [MemoryStatus(str(row["status"])) for row in rows],
                [
                    MemoryStatus.SUPERSEDED,
                    MemoryStatus.SUPERSEDED,
                    MemoryStatus.ACTIVE,
                ],
            )
            check_equal(
                "test_superseded_content_can_become_active_again 检查点 3",
                [int(row["record_version"]) for row in rows],
                [1, 2, 3],
            )
            check_equal(
                "test_superseded_content_can_become_active_again 检查点 4",
                reactivated_a_uid != original_a_uid,
                True,
            )
            check_equal(
                "test_superseded_content_can_become_active_again 检查点 5",
                str(rows[-1]["content_hash"]),
                original_a.content_hash,
            )
            active_count = sum(
                1
                for row in rows
                if MemoryStatus(str(row["status"])) == MemoryStatus.ACTIVE
            )
            check_equal(
                "test_superseded_content_can_become_active_again 检查点 6",
                active_count,
                1,
            )
            outbox_operations = {
                str(uid): str(operation)
                for uid, operation in (
                    await session.execute(
                        select(
                            memory_index_outbox.c.memory_uid,
                            memory_index_outbox.c.operation,
                        ).where(
                            memory_index_outbox.c.memory_uid.in_(
                                {candidate_b_uid, reactivated_a_uid}
                            )
                        )
                    )
                ).all()
            }
            check_equal(
                "test_superseded_content_can_become_active_again 检查点 7",
                outbox_operations,
                {
                    candidate_b_uid: MemoryIndexOperation.DELETE.value,
                    reactivated_a_uid: MemoryIndexOperation.UPSERT.value,
                },
            )
    finally:
        await cleanup_schema(schema)
        await MySQLDatabase.close()


@pytest.mark.integration
async def test_deleted_reactivated_content_is_not_replayed() -> None:
    """验证删除后的版本化 A 不会被原始 A 快照重放复活。"""
    await ensure_schema()
    schema = await parse_ddl(
        f"memory_deleted_replay_{uuid4().hex}",
        "CREATE TABLE dim_customer (id BIGINT PRIMARY KEY, name VARCHAR(64))",
    )
    candidates = build_accepted_memories(
        schema,
        semantic_for(schema, fact=False),
        [],
        [],
        [],
        job_id=uuid4().hex,
    )
    original_a = candidates[0]
    replayed_a = original_a.model_copy(deep=True)
    replayed_after_delete = original_a.model_copy(deep=True)
    if not isinstance(original_a.content, SemanticDecisionContent):
        raise RuntimeError("测试候选必须是语义记忆")
    if original_a.content.table is None:
        raise RuntimeError("测试候选必须包含表语义")
    content_b = original_a.content.model_copy(
        update={
            "table": original_a.content.table.model_copy(
                update={"description": f"修正后的客户维表-{uuid4().hex}"}
            )
        }
    )
    content_b_json = canonical_content_json(content_b)
    candidate_b = original_a.model_copy(
        deep=True,
        update={
            "uid": memory_uid(
                original_a.source,
                original_a.category,
                original_a.memory_key,
                original_a.schema_fingerprint or "",
                content_b_json,
            ),
            "memory_text": build_memory_text(content_b),
            "content": content_b,
            "content_hash": memory_content_hash(content_b),
            "created_job_id": uuid4().hex,
        },
    )
    try:
        async with MySQLDatabase.session() as session:
            await MemoryRepository(session).upsert_candidates([original_a])
        async with MySQLDatabase.session() as session:
            await MemoryRepository(session).upsert_candidates([candidate_b])
        async with MySQLDatabase.session() as session:
            await MemoryRepository(session).upsert_candidates([replayed_a])
            reactivated_a = await MemoryRepository(session).get_by_uid(replayed_a.uid)
            if reactivated_a is None:
                raise RuntimeError("重新生效的 A 必须存在")
            await MemoryRepository(session).soft_delete(reactivated_a)
            deleted_uid = replayed_a.uid
        async with MySQLDatabase.session() as session:
            await MemoryRepository(session).upsert_candidates([replayed_after_delete])
            rows = list(
                (
                    await session.execute(
                        select(agent_memory.c.uid, agent_memory.c.status)
                        .where(
                            agent_memory.c.source == original_a.source,
                            agent_memory.c.category == original_a.category,
                            agent_memory.c.memory_key == original_a.memory_key,
                        )
                        .order_by(agent_memory.c.record_version)
                    )
                ).mappings()
            )
            check_equal(
                "test_deleted_reactivated_content_is_not_replayed 检查点 1",
                [MemoryStatus(str(row["status"])) for row in rows],
                [
                    MemoryStatus.SUPERSEDED,
                    MemoryStatus.SUPERSEDED,
                    MemoryStatus.DELETED,
                ],
            )
            check_equal(
                "test_deleted_reactivated_content_is_not_replayed 检查点 2",
                len(rows),
                3,
            )
            outbox_operation = await session.scalar(
                select(memory_index_outbox.c.operation).where(
                    memory_index_outbox.c.memory_uid == deleted_uid
                )
            )
            check_equal(
                "test_deleted_reactivated_content_is_not_replayed 检查点 3",
                outbox_operation,
                MemoryIndexOperation.DELETE.value,
            )
    finally:
        await cleanup_schema(schema)
        await MySQLDatabase.close()


@pytest.mark.integration
async def test_metric_merge_preserves_complementary_evidence() -> None:
    """验证 MERGE 策略写新版本前保留既有指标问答证据。"""
    await ensure_schema()
    schema = await parse_ddl(
        f"memory_metric_merge_{uuid4().hex}",
        "CREATE TABLE fact_order (id BIGINT PRIMARY KEY, amount DECIMAL(12,2))",
    )
    semantic = semantic_for(schema, fact=True)
    fact_id = schema.tables[0].id
    amount_id = next(
        column.id for column in schema.tables[0].columns if column.name == "amount"
    )
    first_question = MetricQuestion(
        question_id="total_amount.scope",
        prompt="指标统计范围是什么？",
        fact_table_id=fact_id,
        column_ids=[amount_id],
    )
    first_answer = MetricAnswer(
        question_id=first_question.question_id,
        answer="统计所有已支付订单。",
    )
    second_question = MetricQuestion(
        question_id="total_amount.currency",
        prompt="指标金额单位是什么？",
        fact_table_id=fact_id,
        column_ids=[amount_id],
    )
    second_answer = MetricAnswer(
        question_id=second_question.question_id,
        answer="金额单位为人民币元。",
    )
    first_metric = MetricMetadata(
        id=f"metric_{uuid4().hex}",
        name="total_amount",
        fact_table_id=fact_id,
        definition="统计所有已支付订单的 amount 总和。",
        relevant_column_ids=[amount_id],
        answer_question_ids=[first_question.question_id],
    )
    second_metric = first_metric.model_copy(
        update={
            "definition": "统计所有已支付订单的 amount 总和，金额单位为人民币元。",
            "answer_question_ids": [second_question.question_id],
        }
    )
    first_candidates = build_accepted_memories(
        schema,
        semantic,
        [first_question],
        [first_answer],
        [first_metric],
        job_id=uuid4().hex,
    )
    second_candidates = build_accepted_memories(
        schema,
        semantic,
        [second_question],
        [second_answer],
        [second_metric],
        job_id=uuid4().hex,
    )
    try:
        async with MySQLDatabase.session() as session:
            repository = MemoryRepository(session)
            await repository.upsert_candidates(first_candidates)
            await repository.upsert_candidates(second_candidates)
            merged_uid = second_candidates[-1].uid
            repeated_candidates = build_accepted_memories(
                schema,
                semantic,
                [second_question],
                [second_answer],
                [second_metric],
                job_id=uuid4().hex,
            )
            await repository.upsert_candidates(repeated_candidates)
            active_metric = await repository.get_by_uid(merged_uid)
            if active_metric is None:
                raise RuntimeError("合并后的指标记忆必须存在")
            if not isinstance(active_metric.content, MetricDefinitionContent):
                raise RuntimeError("合并后的指标记忆必须是指标内容")
            check_equal(
                "test_metric_merge_preserves_complementary_evidence 检查点 1",
                active_metric.detail.status,
                MemoryStatus.ACTIVE,
            )
            check_equal(
                "test_metric_merge_preserves_complementary_evidence 检查点 2",
                [question.question_id for question in active_metric.content.questions],
                [first_question.question_id, second_question.question_id],
            )
            check_equal(
                "test_metric_merge_preserves_complementary_evidence 检查点 3",
                [answer.question_id for answer in active_metric.content.answers],
                [first_answer.question_id, second_answer.question_id],
            )
            superseded_count = await session.scalar(
                select(func.count())
                .select_from(agent_memory)
                .where(
                    agent_memory.c.source == schema.source,
                    agent_memory.c.category == "ddl.metric",
                    agent_memory.c.memory_key == first_metric.id,
                    agent_memory.c.status == MemoryStatus.SUPERSEDED.value,
                )
            )
            check_equal(
                "test_metric_merge_preserves_complementary_evidence 检查点 4",
                superseded_count,
                1,
            )
            metric_rows = await session.scalar(
                select(func.count())
                .select_from(agent_memory)
                .where(
                    agent_memory.c.source == schema.source,
                    agent_memory.c.category == "ddl.metric",
                    agent_memory.c.memory_key == first_metric.id,
                )
            )
            check_equal(
                "test_metric_merge_preserves_complementary_evidence 检查点 5",
                metric_rows,
                2,
            )
    finally:
        await cleanup_schema(schema)
        await MySQLDatabase.close()


@pytest.mark.integration
async def test_fingerprint_expiry_preserves_unsubmitted_table_scope() -> None:
    """验证局部 DDL 重跑只过期本次提交作用域内的旧指纹记忆。"""
    await ensure_schema()
    source = f"memory_partial_{uuid4().hex}"
    schema = await parse_ddl(
        source,
        """
        CREATE TABLE dim_customer (id BIGINT PRIMARY KEY, name VARCHAR(64));
        CREATE TABLE dim_product (id BIGINT PRIMARY KEY, name VARCHAR(64));
        """,
    )
    candidates = build_accepted_memories(
        schema,
        semantic_for(schema, fact=False),
        [],
        [],
        [],
        job_id=uuid4().hex,
    )
    customer_table = next(
        table for table in schema.tables if table.name == "dim_customer"
    )
    product_table = next(
        table for table in schema.tables if table.name == "dim_product"
    )
    try:
        async with MySQLDatabase.session() as session:
            await MemoryRepository(session).upsert_candidates(candidates)

        partial_schema = await parse_ddl(
            source,
            "CREATE TABLE dim_customer (id BIGINT PRIMARY KEY, full_name VARCHAR(128))",
        )
        partial_fingerprints = {
            object_id: scope_fingerprint(partial_schema, object_id)
            for object_id in (
                *[table.id for table in partial_schema.tables],
                *[
                    column.id
                    for table in partial_schema.tables
                    for column in table.columns
                ],
            )
        }
        async with MySQLDatabase.session() as session:
            expired = await MemoryRepository(session).expire_fingerprint_bound(
                source,
                set(partial_fingerprints.values()),
                memory_keys=set(partial_fingerprints),
            )
            rows = list(
                (
                    await session.execute(
                        select(agent_memory.c.memory_key, agent_memory.c.status).where(
                            agent_memory.c.source == source,
                            agent_memory.c.memory_key.in_(
                                {customer_table.id, product_table.id}
                            ),
                        )
                    )
                ).all()
            )
            status_by_key = {
                str(key): MemoryStatus(str(status)) for key, status in rows
            }
            check_equal(
                "test_fingerprint_expiry_preserves_unsubmitted_table_scope 检查点 1",
                expired,
                1,
            )
            check_equal(
                "test_fingerprint_expiry_preserves_unsubmitted_table_scope 检查点 2",
                status_by_key[customer_table.id],
                MemoryStatus.EXPIRED,
            )
            check_equal(
                "test_fingerprint_expiry_preserves_unsubmitted_table_scope 检查点 3",
                status_by_key[product_table.id],
                MemoryStatus.ACTIVE,
            )
    finally:
        await cleanup_schema(schema)
        await MySQLDatabase.close()
