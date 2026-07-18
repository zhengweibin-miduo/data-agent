"""Meta 快照范围、幂等性和事务回滚检查。"""

from typing import cast
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import FromClause

from data_agent.ddl_metadata.memory.snapshots import MetadataSnapshotService
from data_agent.ddl_metadata.models import (
    MemoryCandidate,
    MemoryKind,
    MemoryRowStatus,
    SemanticDecisionContent,
)
from data_agent.ddl_metadata.parsing import parse_ddl
from data_agent.ddl_metadata.persistence.memory_repository import MemoryRepository
from data_agent.ddl_metadata.persistence.tables import (
    column_info,
    column_metric,
    llm_memory,
    llm_memory_relation,
    metric_info,
    table_info,
)
from data_agent.infrastructure.mysql import MySQLDatabase
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


async def _count(
    table: FromClause,
    condition: ColumnElement[bool],
) -> int:
    """执行单表条件计数。"""
    async with MySQLDatabase.session() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(table).where(condition)
            )
            or 0
        )


async def _force_memory_database_failure(
    repository: MemoryRepository,
    candidates: list[MemoryCandidate],
) -> None:
    """在应用记忆库执行真实约束失败。"""
    table_id = next(
        candidate.scope_key
        for candidate in candidates
        if isinstance(candidate.content, SemanticDecisionContent)
        and candidate.content.table is not None
    )
    check_equal(
        "_force_memory_database_failure 检查点 1",
        await repository._session.scalar(
            select(func.count())
            .select_from(table_info)
            .where(table_info.c.id == table_id)
        ),
        1,
    )
    await repository._session.execute(
        insert(llm_memory).values(
            uid=None,
            source="forced_failure",
            kind=MemoryKind.SEMANTIC_DECISION.value,
            scope_key="forced_failure",
            schema_fingerprint="0" * 64,
            row_status=MemoryRowStatus.NORMAL.value,
            pinned=False,
            content={},
            payload={},
            content_version=app_config.memory.content_version,
        )
    )


async def _test_meta_repository() -> None:
    """验证重复、变更范围、无关保留和 Meta+记忆回滚。"""
    await ensure_schema()
    check_condition(
        "_test_meta_repository 检查点 1",
        all(
            table.schema is None
            for table in (table_info, column_info, metric_info, column_metric)
        ),
        expected="原断言条件成立",
    )
    check_equal(
        "_test_meta_repository 检查点 2",
        llm_memory.schema,
        app_config.memory.database,
    )
    check_equal(
        "_test_meta_repository 检查点 3",
        llm_memory_relation.schema,
        app_config.memory.database,
    )
    check_equal(
        "_test_meta_repository 检查点 4",
        llm_memory.fullname,
        f"{app_config.memory.database}.llm_memory",
    )
    check_equal(
        "_test_meta_repository 检查点 5",
        llm_memory_relation.fullname,
        f"{app_config.memory.database}.llm_memory_relation",
    )
    default_database = make_url(app_config.mysql.url).database
    check_condition(
        "_test_meta_repository 检查点 6",
        default_database is not None,
        expected="原断言条件成立",
    )
    default_database = cast(str, default_database)
    check_condition(
        "_test_meta_repository 检查点 7",
        default_database.casefold() != app_config.memory.database.casefold(),
        expected="原断言条件成立",
    )
    source = f"meta_{uuid4().hex}"
    unrelated_source = f"meta_{uuid4().hex}"
    rollback_source = f"meta_{uuid4().hex}"
    initial = parse_ddl(
        source,
        """
        CREATE TABLE fact_snapshot (
            id BIGINT PRIMARY KEY,
            category VARCHAR(20),
            amount DECIMAL(10,2)
        )
        """,
    )
    changed = parse_ddl(
        source,
        """
        CREATE TABLE fact_snapshot (
            id BIGINT PRIMARY KEY,
            category VARCHAR(20)
        )
        """,
    )
    unrelated = parse_ddl(
        unrelated_source,
        "CREATE TABLE dim_unrelated (id BIGINT PRIMARY KEY, name VARCHAR(20))",
    )
    rollback = parse_ddl(
        rollback_source,
        "CREATE TABLE dim_rollback (id BIGINT PRIMARY KEY)",
    )
    service = MetadataSnapshotService()
    questions, answers, metrics = metric_bundle(initial)
    metric_identifier = metrics[0].id
    amount_id = metrics[0].relevant_column_ids[0]
    try:
        await service.persist(
            initial,
            semantic_for(initial, fact=True),
            questions,
            answers,
            metrics,
        )
        await service.persist(
            initial,
            semantic_for(initial, fact=True),
            questions,
            answers,
            metrics,
        )
        table_ids = [table.id for table in initial.tables]
        column_ids = [column.id for table in initial.tables for column in table.columns]
        check_equal(
            "_test_meta_repository 检查点 8",
            await _count(
                table_info,
                table_info.c.id.in_(table_ids),
            ),
            1,
        )
        check_equal(
            "_test_meta_repository 检查点 9",
            await _count(
                column_info,
                column_info.c.id.in_(column_ids),
            ),
            3,
        )
        check_equal(
            "_test_meta_repository 检查点 10",
            await _count(
                metric_info,
                metric_info.c.id == metric_identifier,
            ),
            1,
        )
        check_equal(
            "_test_meta_repository 检查点 11",
            await _count(
                column_metric,
                column_metric.c.metric_id == metric_identifier,
            ),
            1,
        )
        check_condition(
            "_test_meta_repository 检查点 12",
            await _count(
                llm_memory,
                llm_memory.c.source == source,
            )
            >= 6,
            expected="原断言条件成立",
        )

        await service.persist(
            unrelated,
            semantic_for(unrelated, fact=False),
            [],
            [],
            [],
        )
        await service.persist(
            changed,
            semantic_for(changed, fact=False),
            [],
            [],
            [],
        )
        check_equal(
            "_test_meta_repository 检查点 13",
            await _count(
                column_info,
                column_info.c.id == amount_id,
            ),
            0,
        )
        check_equal(
            "_test_meta_repository 检查点 14",
            await _count(
                column_metric,
                column_metric.c.metric_id == metric_identifier,
            ),
            0,
        )
        check_equal(
            "_test_meta_repository 检查点 15",
            await _count(
                metric_info,
                metric_info.c.id == metric_identifier,
            ),
            0,
        )
        check_equal(
            "_test_meta_repository 检查点 16",
            await _count(
                table_info,
                table_info.c.id == unrelated.tables[0].id,
            ),
            1,
        )
        check_equal(
            "_test_meta_repository 检查点 17",
            await _count(
                llm_memory,
                (
                    (llm_memory.c.source == source)
                    & (llm_memory.c.kind == MemoryKind.SEMANTIC_DECISION.value)
                    & (llm_memory.c.scope_key == changed.tables[0].id)
                    & (llm_memory.c.row_status == MemoryRowStatus.NORMAL.value)
                ),
            ),
            1,
        )

        with patch.object(
            MemoryRepository,
            "upsert_candidates",
            _force_memory_database_failure,
        ):
            try:
                await service.persist(
                    rollback,
                    semantic_for(rollback, fact=False),
                    [],
                    [],
                    [],
                )
            except IntegrityError as captured_error:
                check_exception(
                    "_test_meta_repository 捕获预期异常", captured_error, IntegrityError
                )
                pass
            else:
                fail_check(
                    "_test_meta_repository",
                    actual="未抛出预期异常",
                    expected="应用记忆库失败必须回滚 Meta",
                )
        check_equal(
            "_test_meta_repository 检查点 18",
            await _count(
                table_info,
                table_info.c.id == rollback.tables[0].id,
            ),
            0,
        )
        check_equal(
            "_test_meta_repository 检查点 19",
            await _count(
                llm_memory,
                llm_memory.c.source == rollback_source,
            ),
            0,
        )
    finally:
        await cleanup_schema(initial)
        await cleanup_schema(unrelated)
        await cleanup_schema(rollback)
        await MySQLDatabase.close()


@pytest.mark.integration
async def test_meta_repository() -> None:
    """运行真实 MySQL Meta 仓储检查。"""
    await _test_meta_repository()
