"""Meta、权威记忆与 outbox 原子事务集成检查。"""

from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError

from data_agent.ddl_metadata.memory.application.snapshots import (
    MetadataSnapshotService,
)
from data_agent.ddl_metadata.memory.mysql.repository import MemoryRepository
from data_agent.ddl_metadata.memory.mysql.tables import (
    agent_memory,
    agent_memory_event,
    agent_memory_link,
    memory_index_outbox,
)
from data_agent.ddl_metadata.models.memory import MemoryCandidate
from data_agent.ddl_metadata.parsing import parse_ddl
from data_agent.ddl_metadata.persistence.tables import (
    table_info,
)
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.settings import app_config
from tests.helpers.checks import check_equal, check_exception, fail_check
from tests.helpers.factories import cleanup_schema, ensure_schema, semantic_for


async def _force_memory_failure(
    self: MemoryRepository,
    candidates: list[MemoryCandidate],
) -> None:
    """在 Meta 写入后制造真实记忆侧非空约束失败。"""
    del candidates
    await self._session.execute(
        insert(agent_memory).values(
            uid=None,
            source="forced_failure",
            kind="SEMANTIC_DECISION",
            scope_key="forced_failure",
            schema_fingerprint="0" * 64,
            memory_text="forced",
            content={},
            content_hash="0" * 64,
            trust="model_validated",
            status="ACTIVE",
            content_version=app_config.memory.content_version,
            projection_version=app_config.memory.projection_version,
            created_job_id="forced",
        )
    )


@pytest.mark.integration
async def test_meta_memory_outbox_atomicity() -> None:
    """验证新表归属、重复执行幂等和记忆侧失败回滚 Meta。"""
    await ensure_schema()
    check_equal(
        "test_meta_memory_outbox_atomicity 检查点 1",
        [
            table.name
            for table in (
                agent_memory,
                agent_memory_event,
                agent_memory_link,
                memory_index_outbox,
            )
        ],
        [
            "agent_memory",
            "agent_memory_event",
            "agent_memory_link",
            "memory_index_outbox",
        ],
    )
    check_equal(
        "test_meta_memory_outbox_atomicity 检查点 2",
        agent_memory.schema,
        app_config.memory.database,
    )
    source = f"atomic_{uuid4().hex}"
    rollback_source = f"rollback_{uuid4().hex}"
    schema = await parse_ddl(
        source,
        "CREATE TABLE dim_product (id BIGINT PRIMARY KEY, name VARCHAR(64))",
    )
    rollback_schema = await parse_ddl(
        rollback_source,
        "CREATE TABLE dim_rollback (id BIGINT PRIMARY KEY)",
    )
    service = MetadataSnapshotService()
    try:
        await service.persist(
            schema,
            semantic_for(schema, fact=False),
            [],
            [],
            [],
        )
        await service.persist(
            schema,
            semantic_for(schema, fact=False),
            [],
            [],
            [],
        )
        async with MySQLDatabase.session() as session:
            memory_count = await session.scalar(
                select(func.count())
                .select_from(agent_memory)
                .where(agent_memory.c.source == source)
            )
            event_count = await session.scalar(
                select(func.count())
                .select_from(agent_memory_event)
                .join(
                    agent_memory,
                    agent_memory.c.id == agent_memory_event.c.memory_id,
                )
                .where(agent_memory.c.source == source)
            )
        check_equal(
            "test_meta_memory_outbox_atomicity 检查点 3",
            memory_count,
            event_count,
        )

        original = MemoryRepository.upsert_candidates
        MemoryRepository.upsert_candidates = _force_memory_failure
        try:
            try:
                await service.persist(
                    rollback_schema,
                    semantic_for(rollback_schema, fact=False),
                    [],
                    [],
                    [],
                )
            except IntegrityError as error:
                check_exception(
                    "test_meta_memory_outbox_atomicity 捕获预期异常",
                    error,
                    IntegrityError,
                )
            else:
                fail_check(
                    "test_meta_memory_outbox_atomicity",
                    actual="未抛出异常",
                    expected="记忆写入失败必须回滚 Meta",
                )
        finally:
            MemoryRepository.upsert_candidates = original
        async with MySQLDatabase.session() as session:
            rolled_back = await session.scalar(
                select(func.count())
                .select_from(table_info)
                .where(table_info.c.id == rollback_schema.tables[0].id)
            )
        check_equal(
            "test_meta_memory_outbox_atomicity 检查点 4",
            rolled_back,
            0,
        )
    finally:
        await cleanup_schema(schema)
        await cleanup_schema(rollback_schema)
        await MySQLDatabase.close()
