"""记忆重建服务的真实 MySQL 检查。"""

from uuid import uuid4

import pytest
from sqlalchemy import func, select

from data_agent.ddl_metadata.memory.outbox import MemoryIndexRebuilder
from data_agent.ddl_metadata.memory.snapshots import MetadataSnapshotService
from data_agent.ddl_metadata.parsing import parse_ddl
from data_agent.ddl_metadata.persistence.tables import memory_index_outbox
from data_agent.infrastructure.mysql import MySQLDatabase
from tests.helpers.checks import check_condition, check_equal
from tests.helpers.factories import cleanup_schema, ensure_schema, semantic_for


@pytest.mark.integration
async def test_memory_rebuild_enqueue() -> None:
    """验证全量重建仅从活动 MySQL 权威记忆生成 outbox。"""
    await ensure_schema()
    schema = parse_ddl(
        f"rebuild_{uuid4().hex}",
        "CREATE TABLE dim_region (id BIGINT PRIMARY KEY, name VARCHAR(64))",
    )
    try:
        await MetadataSnapshotService().persist(
            schema,
            semantic_for(schema, fact=False),
            [],
            [],
            [],
        )
        result = await MemoryIndexRebuilder().enqueue_batch()
        check_condition(
            "test_memory_rebuild_enqueue 检查点 1",
            result.processed >= 2,
            expected="至少扫描当前表列的两条活动记忆",
        )
        async with MySQLDatabase.session() as session:
            count = await session.scalar(
                select(func.count()).select_from(memory_index_outbox)
            )
        check_condition(
            "test_memory_rebuild_enqueue 检查点 2",
            (count or 0) >= 4,
            expected="每条活动记忆包含 ES 与 Qdrant 两个期望状态",
        )
        check_equal(
            "test_memory_rebuild_enqueue 检查点 3",
            result.processed > 0,
            True,
        )
    finally:
        await cleanup_schema(schema)
        await MySQLDatabase.close()
