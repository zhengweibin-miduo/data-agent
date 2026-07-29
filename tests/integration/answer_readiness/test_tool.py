"""真实 data_sync 状态的只读就绪工具检查。"""

from uuid import uuid4

import pytest
from sqlalchemy import delete, func, insert, select

from data_agent.answer_readiness.tool import create_data_readiness_tool
from data_agent.data_sync.models import SyncPhase
from data_agent.data_sync.tables import data_sync_task
from data_agent.infrastructure.mysql import MySQLDatabase
from tests.helpers.checks import check_equal


@pytest.mark.integration
async def test_tool_reads_real_state_without_modifying_tasks() -> None:
    """工具读取来源和汇总状态后保持完整任务记录不变。"""
    suffix = uuid4().hex[:12]
    target_table = f"ready_{suffix}"
    source_ready = f"ready_source_{suffix}"
    source_paused = f"paused_source_{suffix}"
    MySQLDatabase.initialize()
    try:
        # 步骤一：写入本测试独占的一条就绪任务和一条未就绪任务。
        async with MySQLDatabase.session() as session:
            await session.execute(
                insert(data_sync_task),
                [
                    {
                        "source": source_ready,
                        "source_schema": "source_demo",
                        "source_table": f"ready_table_{suffix}",
                        "target_table": target_table,
                        "desired_json": {},
                        "desired_hash": "a" * 64,
                        "phase": SyncPhase.STREAMING.value,
                        "worker_heartbeat_at": func.now(),
                        "attempts": 2,
                        "lease_token": f"{suffix:0<32}"[:32],
                        "last_error_type": "safe_error",
                    },
                    {
                        "source": source_paused,
                        "source_schema": "source_demo",
                        "source_table": f"paused_table_{suffix}",
                        "target_table": target_table,
                        "desired_json": {},
                        "desired_hash": "b" * 64,
                        "phase": SyncPhase.PAUSED.value,
                        "worker_heartbeat_at": None,
                        "attempts": 3,
                        "lease_token": None,
                        "last_error_type": "paused_error",
                    },
                ],
            )
        async with MySQLDatabase.session() as session:
            before = [
                dict(row)
                for row in (
                    await session.execute(
                        select(data_sync_task)
                        .where(data_sync_task.c.target_table == target_table)
                        .order_by(data_sync_task.c.id)
                    )
                ).mappings()
            ]

        # 步骤二：来源限定只检查就绪任务；汇总检查全部来源并被暂停任务阻断。
        tool = create_data_readiness_tool()
        scoped = await tool.ainvoke(
            {"dependencies": [{"target_table": target_table, "source": source_ready}]}
        )
        aggregate = await tool.ainvoke(
            {"dependencies": [{"target_table": target_table, "source": None}]}
        )
        check_equal("真实来源限定任务就绪", scoped, {"ready": True})
        check_equal("真实汇总任务未全部就绪", aggregate, {"ready": False})

        # 步骤三：复读完整控制记录，证明阶段、租约、重试、位点和时间均未变化。
        async with MySQLDatabase.session() as session:
            after = [
                dict(row)
                for row in (
                    await session.execute(
                        select(data_sync_task)
                        .where(data_sync_task.c.target_table == target_table)
                        .order_by(data_sync_task.c.id)
                    )
                ).mappings()
            ]
        check_equal("工具调用前后任务完整记录", after, before)
    finally:
        # 步骤四：只清理本测试 UUID 命名的控制任务。
        async with MySQLDatabase.session() as session:
            await session.execute(
                delete(data_sync_task).where(
                    data_sync_task.c.target_table == target_table
                )
            )
        await MySQLDatabase.close()
