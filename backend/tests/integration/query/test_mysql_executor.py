"""专用 DW SELECT-only executor 的真实 MySQL 契约测试。"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from ddl_metadata.parsing import parse_ddl
from query.adapters.mysql import MySQLQueryExecutor
from query.domain import (
    QueryContext,
    QueryDraft,
    QueryIntent,
    QueryType,
    validate_query,
)
from settings import app_config

pytestmark = pytest.mark.integration


async def test_select_only_user_explains_and_streams_explicit_top_n() -> None:
    """专用账号只读预检并按批次返回用户明确的 Top-N。"""
    schema = await parse_ddl(
        "source_demo",
        "CREATE TABLE dim_region (region_id VARCHAR(20) PRIMARY KEY)",
    )
    table = schema.tables[0]
    column = table.columns[0]
    result = await validate_query(
        QueryDraft(
            sql="SELECT r.region_id FROM dw.dim_region AS r LIMIT 2",
            table_ids=[table.id],
            column_ids=[column.id],
        ),
        QueryContext(physical_schema=schema),
        QueryIntent(query_type=QueryType.DETAIL, limit=2),
        dw_database=app_config.data_sync.dw_database,
    )
    assert result.validated is not None
    executor = MySQLQueryExecutor(
        app_config.query.read_url,
        timeout_seconds=app_config.query.timeout_seconds,
        fetch_batch_rows=1,
        max_batch_bytes=app_config.query.max_batch_bytes,
    )
    try:
        await executor.explain(result.validated)
        batches = [batch async for batch in executor.execute(result.validated)]
    finally:
        await executor.close()

    assert all(len(batch.rows) <= 1 for batch in batches)
    assert sum(len(batch.rows) for batch in batches) == 2


async def test_select_only_database_grants_reject_write_and_meta_read() -> None:
    """数据库最终防线拒绝 DW 写入和非 DW schema 读取。"""
    engine = create_async_engine(app_config.query.read_url)
    try:
        async with engine.connect() as connection:
            with pytest.raises(DBAPIError):
                await connection.execute(text("DELETE FROM dw.dim_region WHERE 1 = 0"))
            await connection.rollback()
            with pytest.raises(DBAPIError):
                await connection.execute(text("SELECT 1 FROM meta.table_info LIMIT 1"))
            await connection.rollback()
    finally:
        await engine.dispose()
