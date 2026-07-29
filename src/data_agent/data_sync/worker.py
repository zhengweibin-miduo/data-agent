"""独立 MySQL Binlog CDC 进程入口。"""

from __future__ import annotations

import asyncio

from loguru import logger
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from data_agent.data_sync.binlog import MySQLSourceClient, close_sources
from data_agent.data_sync.repository import DataSyncRepository
from data_agent.data_sync.service import DataSyncService
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.logging import logging_boundary, setup_logging
from data_agent.settings import app_config


async def run_worker() -> None:
    """初始化资源并持续执行有界数据同步步骤。"""
    setup_logging()
    MySQLDatabase.initialize()
    sources = {
        name: MySQLSourceClient(
            name,
            settings,
            connect_timeout_seconds=app_config.data_sync.source_connect_timeout_seconds,
            read_timeout_seconds=app_config.data_sync.source_read_timeout_seconds,
        )
        for name, settings in app_config.data_sync.sources.items()
    }
    service = DataSyncService(sources, app_config.data_sync)
    try:
        # 步骤一：启动前验证全部源满足 ROW/FULL Binlog 契约。
        await asyncio.gather(
            *(source.acquire_worker_lock() for source in sources.values())
        )
        await asyncio.gather(
            *(source.check_capabilities() for source in sources.values())
        )
        async with MySQLDatabase.session() as session:
            await _check_dw_table_name_case_sensitivity(session)
            desired_tables = await DataSyncRepository(session).read_desired_tables()
        await asyncio.gather(
            *(
                sources[item.source].check_select_access(
                    item.source_schema, item.source_table
                )
                for item in desired_tables
                if item.source in sources
            )
        )
        logger.info("数据同步进程启动成功，开始消费 DW 结构与 Binlog 任务")
        # 步骤二：每轮只执行有界任务步骤；空闲时按配置暂停，避免忙轮询。
        while True:
            try:
                processed = await service.dispatch_once()
            except (ConnectionError, OSError, TimeoutError, SQLAlchemyError):
                logger.exception("数据同步控制库轮询失败，将在退避后重试")
                await asyncio.sleep(app_config.data_sync.poll_interval_seconds)
                continue
            if processed == 0:
                await asyncio.sleep(app_config.data_sync.poll_interval_seconds)
    finally:
        # 步骤三：逆序释放源连接和目标连接，确保队列日志在退出前落盘。
        try:
            await close_sources(sources.values())
        finally:
            await MySQLDatabase.close()
            logger.info("数据同步进程已停止并释放全部数据库连接")
            await logger.complete()


async def _check_dw_table_name_case_sensitivity(session: AsyncSession) -> None:
    """拒绝目标表名大小写不敏感的 DW，避免控制面身份发生别名碰撞。"""
    mode = int(
        (await session.execute(text("SELECT @@lower_case_table_names"))).scalar_one()
    )
    if mode != 0:
        raise RuntimeError(
            "DW MySQL 必须配置 lower_case_table_names=0，"
            "否则目标表身份无法与控制面二进制名称保持一致"
        )


def main() -> None:
    """启动带 AOP 日志上下文的独立 CDC 进程。"""
    wrapped = logging_boundary(
        component="data_sync.worker",
        operation="run_worker",
    )(run_worker)
    asyncio.run(wrapped())


if __name__ == "__main__":
    main()
