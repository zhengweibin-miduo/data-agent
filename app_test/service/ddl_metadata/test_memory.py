"""长期记忆派生载荷重建检查。"""

import asyncio
from uuid import uuid4

from sqlalchemy import delete, insert, select

from app.client.mysql_client_manager import MysqlClientManager
from app.conf.app_config import app_config
from app.model.ddl_metadata import (
    MemoryKind,
    MemoryRowStatus,
    SemanticDecisionContent,
    SemanticTable,
    TableRole,
)
from app.repository.ddl_metadata.schema import llm_memory
from app.service.ddl_metadata.identifiers import stable_id
from app.service.ddl_metadata.memory import (
    MemoryPayloadRebuilder,
    build_memory_payload,
)
from app_test.repository.ddl_metadata.fixtures import ensure_schema


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
    assert content.table is not None
    valid_uid = stable_id("memory", source, "valid")
    invalid_uid = stable_id("memory", source, "invalid")
    try:
        async with MysqlClientManager.session() as session:
            await session.execute(
                insert(llm_memory),
                [
                    {
                        "uid": valid_uid,
                        "source": source,
                        "kind": MemoryKind.SEMANTIC_DECISION.value,
                        "scope_key": content.table.table_id,
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
        assert first.processed == 1
        assert first.succeeded == 1
        assert first.failed == 0
        assert first.next_after_id is not None
        second = await MemoryPayloadRebuilder().rebuild(
            source,
            after_id=first.next_after_id,
        )
        assert second.processed == 1
        assert second.succeeded == 0
        assert second.failed == 1
        assert second.next_after_id is not None
        finished = await MemoryPayloadRebuilder().rebuild(
            source,
            after_id=second.next_after_id,
        )
        assert finished.processed == 0
        assert finished.next_after_id is None
        async with MysqlClientManager.session() as session:
            values = (
                await session.execute(
                    select(
                        llm_memory.c.uid,
                        llm_memory.c.content,
                        llm_memory.c.payload,
                    ).where(llm_memory.c.source == source)
                )
            ).all()
            rows = {
                str(row[0]): (row[1], row[2])
                for row in values
            }
        assert rows[invalid_uid][0] == {"bad": "content"}
        assert rows[valid_uid][1]["version"] == app_config.memory.payload_version
    finally:
        app_config.memory.rebuild_batch_size = original_batch_size
        async with MysqlClientManager.session() as session:
            await session.execute(
                delete(llm_memory).where(llm_memory.c.source == source)
            )
        await MysqlClientManager.close()


def test_memory_payload_rebuilder() -> None:
    """运行真实 MySQL 载荷重建检查。"""
    asyncio.run(_test_payload_rebuilder())


if __name__ == "__main__":
    test_memory_payload_rebuilder()
