"""只读数据就绪 LangChain 工具检查。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.answer_readiness.models import DataReadinessToolInput
from data_agent.answer_readiness.tool import (
    DATA_READINESS_TOOL_NAME,
    create_data_readiness_tool,
)
from data_agent.data_sync.models import SyncPhase
from data_agent.data_sync.repository import DataSyncRepository
from data_agent.infrastructure.mysql import MySQLDatabase
from tests.helpers.checks import check_condition, check_equal


@asynccontextmanager
async def _fake_session() -> AsyncIterator[AsyncSession]:
    """提供无需数据库连接的 Session 占位。"""
    yield AsyncMock(spec=AsyncSession)


async def test_tool_schema_and_result_expose_only_safe_fields(monkeypatch) -> None:
    """工具参数与结果不包含任务状态、凭证、租约或业务行。"""
    read_phases = AsyncMock(return_value=[(SyncPhase.STREAMING, True)])
    monkeypatch.setattr(MySQLDatabase, "session", _fake_session)
    monkeypatch.setattr(DataSyncRepository, "read_readiness_phases", read_phases)
    tool = create_data_readiness_tool()
    result = await tool.ainvoke(
        {"dependencies": [{"target_table": "orders", "source": "erp"}]}
    )
    args_schema = cast(type[DataReadinessToolInput], tool.args_schema)
    schema_text = str(args_schema.model_json_schema())
    check_equal("工具稳定名称", tool.name, DATA_READINESS_TOOL_NAME)
    check_equal("工具结果仅包含 ready", result, {"ready": True})
    for sensitive in (
        "lease_token",
        "desired_json",
        "password",
        "binlog",
        "attempts",
    ):
        check_equal(
            f"工具 schema 排除 {sensitive}",
            sensitive in schema_text.lower(),
            False,
        )


async def test_tool_requires_every_dependency_and_all_sources_ready(
    monkeypatch,
) -> None:
    """任一依赖未就绪即关闭门禁，来源限定必须唯一匹配。"""
    read_phases = AsyncMock(
        side_effect=[
            [(SyncPhase.STREAMING, True)],
            [(SyncPhase.STREAMING, True), (SyncPhase.PAUSED, True)],
        ]
    )
    monkeypatch.setattr(MySQLDatabase, "session", _fake_session)
    monkeypatch.setattr(DataSyncRepository, "read_readiness_phases", read_phases)
    result = await create_data_readiness_tool().ainvoke(
        {
            "dependencies": [
                {"target_table": "customers", "source": "crm"},
                {"target_table": "orders", "source": None},
            ]
        }
    )
    check_equal("任一来源未就绪", result, {"ready": False})
    check_equal("检查到第二个依赖后停止", read_phases.await_count, 2)


async def test_tool_fails_closed_for_missing_or_ambiguous_source(monkeypatch) -> None:
    """无任务或来源限定后匹配多个任务均视为未就绪。"""
    read_phases = AsyncMock(
        side_effect=[[], [(SyncPhase.STREAMING, True), (SyncPhase.STREAMING, True)]]
    )
    monkeypatch.setattr(MySQLDatabase, "session", _fake_session)
    monkeypatch.setattr(DataSyncRepository, "read_readiness_phases", read_phases)
    tool = create_data_readiness_tool()
    missing = await tool.ainvoke(
        {"dependencies": [{"target_table": "missing", "source": None}]}
    )
    ambiguous = await tool.ainvoke(
        {"dependencies": [{"target_table": "orders", "source": "erp"}]}
    )
    check_equal("无任务关闭门禁", missing, {"ready": False})
    check_equal("来源匹配不唯一关闭门禁", ambiguous, {"ready": False})
    check_condition(
        "两种失败均执行查询",
        read_phases.await_count == 2,
        actual=read_phases.await_count,
        expected=2,
    )


async def test_tool_fails_closed_for_stale_streaming_worker(monkeypatch) -> None:
    """任务虽为 streaming，worker 心跳过期后仍关闭门禁。"""
    read_phases = AsyncMock(return_value=[(SyncPhase.STREAMING, False)])
    monkeypatch.setattr(MySQLDatabase, "session", _fake_session)
    monkeypatch.setattr(DataSyncRepository, "read_readiness_phases", read_phases)

    result = await create_data_readiness_tool().ainvoke(
        {"dependencies": [{"target_table": "orders", "source": "erp"}]}
    )

    check_equal("过期 worker 心跳关闭门禁", result, {"ready": False})
