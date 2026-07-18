"""长期记忆派生载荷重建检查。"""

from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, select

from data_agent.ddl_metadata.identifiers import stable_id
from data_agent.ddl_metadata.memory.payloads import (
    MemoryPayloadRebuilder,
    build_memory_payload,
)
from data_agent.ddl_metadata.models import (
    MemoryKind,
    MemoryRowStatus,
    SemanticDecisionContent,
    SemanticTable,
    TableRole,
)
from data_agent.ddl_metadata.persistence.tables import llm_memory
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.settings import app_config
from tests.helpers.checks import check_condition, check_equal
from tests.helpers.factories import ensure_schema


async def _test_payload_rebuilder() -> None:
    """验证规范内容保留、单行失败隔离和成功计数。"""
    await ensure_schema()
    original_batch_size = app_config.memory.rebuild_batch_size
    app_config.memory.rebuild_batch_size = 1
    source = f"rebuild_{uuid4().hex}"
    content = SemanticDecisionContent(
        kind=MemoryKind.SEMANTIC_DECISION,
        table=SemanticTable(
            table_id=stable_id("table", source, "table"),
            role=TableRole.DIM,
            description="dimension",
            confidence=0.99,
            evidence=[stable_id("table", source, "table")],
        ),
    )
    payload = build_memory_payload(content)
    check_condition(
        "_test_payload_rebuilder 检查点 1",
        content.table is not None,
        expected="原断言条件成立",
    )
    content_table = cast(SemanticTable, content.table)
    valid_uid = stable_id("memory", source, "valid")
    invalid_uid = stable_id("memory", source, "invalid")
    try:
        async with MySQLDatabase.session() as session:
            await session.execute(
                insert(llm_memory),
                [
                    {
                        "uid": valid_uid,
                        "source": source,
                        "kind": MemoryKind.SEMANTIC_DECISION.value,
                        "scope_key": content_table.table_id,
                        "schema_fingerprint": stable_id("schema", source),
                        "row_status": MemoryRowStatus.NORMAL.value,
                        "pinned": False,
                        "content": content.model_dump(mode="json"),
                        "payload": {"bad": "derived payload"},
                        "content_version": app_config.memory.content_version,
                    },
                    {
                        "uid": invalid_uid,
                        "source": source,
                        "kind": MemoryKind.SEMANTIC_DECISION.value,
                        "scope_key": "invalid",
                        "schema_fingerprint": stable_id("schema", source),
                        "row_status": MemoryRowStatus.NORMAL.value,
                        "pinned": False,
                        "content": {"bad": "content"},
                        "payload": payload.model_dump(mode="json"),
                        "content_version": app_config.memory.content_version,
                    },
                ],
            )
        first = await MemoryPayloadRebuilder().rebuild(source)
        check_equal("_test_payload_rebuilder 检查点 2", first.processed, 1)
        check_equal("_test_payload_rebuilder 检查点 3", first.succeeded, 1)
        check_equal("_test_payload_rebuilder 检查点 4", first.failed, 0)
        check_condition(
            "_test_payload_rebuilder 检查点 5",
            first.next_after_id is not None,
            expected="原断言条件成立",
        )
        first_next_after_id = cast(int, first.next_after_id)
        second = await MemoryPayloadRebuilder().rebuild(
            source,
            after_id=first_next_after_id,
        )
        check_equal("_test_payload_rebuilder 检查点 6", second.processed, 1)
        check_equal("_test_payload_rebuilder 检查点 7", second.succeeded, 0)
        check_equal("_test_payload_rebuilder 检查点 8", second.failed, 1)
        check_condition(
            "_test_payload_rebuilder 检查点 9",
            second.next_after_id is not None,
            expected="原断言条件成立",
        )
        second_next_after_id = cast(int, second.next_after_id)
        finished = await MemoryPayloadRebuilder().rebuild(
            source,
            after_id=second_next_after_id,
        )
        check_equal("_test_payload_rebuilder 检查点 10", finished.processed, 0)
        check_condition(
            "_test_payload_rebuilder 检查点 11",
            finished.next_after_id is None,
            expected="原断言条件成立",
        )
        async with MySQLDatabase.session() as session:
            values = (
                await session.execute(
                    select(
                        llm_memory.c.uid,
                        llm_memory.c.content,
                        llm_memory.c.payload,
                    ).where(llm_memory.c.source == source)
                )
            ).all()
            rows = {str(row[0]): (row[1], row[2]) for row in values}
        check_equal(
            "_test_payload_rebuilder 检查点 12",
            rows[invalid_uid][0],
            {"bad": "content"},
        )
        check_equal(
            "_test_payload_rebuilder 检查点 13",
            rows[valid_uid][1]["version"],
            app_config.memory.payload_version,
        )
    finally:
        app_config.memory.rebuild_batch_size = original_batch_size
        async with MySQLDatabase.session() as session:
            await session.execute(
                delete(llm_memory).where(llm_memory.c.source == source)
            )
        await MySQLDatabase.close()


@pytest.mark.integration
async def test_memory_payload_rebuilder() -> None:
    """运行真实 MySQL 载荷重建检查。"""
    await _test_payload_rebuilder()
