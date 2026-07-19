"""Mem0 风格权威记忆仓储集成检查。"""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from data_agent.ddl_metadata.memory.domain.candidates import build_accepted_memories
from data_agent.ddl_metadata.memory.mysql.repository import MemoryRepository
from data_agent.ddl_metadata.memory.mysql.tables import memory_index_outbox
from data_agent.ddl_metadata.models.memory import (
    MemoryEventType,
    MemoryIndexOperation,
    MemoryIndexTarget,
    MemoryStatus,
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
                [MemoryEventType.ADD],
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
                [MemoryEventType.ADD, MemoryEventType.DELETE],
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
