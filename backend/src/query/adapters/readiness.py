"""AST 实际目标表到既有 Answer Readiness 工具的适配器。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langchain_core.tools import BaseTool
from loguru import logger

from answer_readiness.models import DataReadinessToolResult
from data_sync.locks import generation_lock_name
from errors import DataAgentError
from infrastructure.mysql import (
    AdvisoryLockReleaseError,
    AdvisoryLockUnavailableError,
    MySQLDatabase,
)


class QueryReadinessAdapter:
    """直接检查最终目标表，避免模型再次改写已经解析的依赖。"""

    def __init__(self, tool: BaseTool, *, dw_database: str, lock_timeout: int) -> None:
        """绑定现有只读数据就绪工具。"""
        self._tool = tool
        self._dw_database = dw_database
        self._lock_timeout = lock_timeout

    async def ready(self, target_tables: tuple[str, ...]) -> bool:
        """仅当全部统一 DW 目标表均 streaming 时返回真。"""
        raw = await self._tool.ainvoke(
            {
                "dependencies": [
                    {"target_table": target_table, "source": None}
                    for target_table in target_tables
                ]
            }
        )
        return DataReadinessToolResult.model_validate(raw).ready

    @asynccontextmanager
    async def hold(self, target_tables: tuple[str, ...]) -> AsyncIterator[None]:
        """持有与同步重建共享的 generation READ locks。"""
        names = [
            generation_lock_name(self._dw_database, table) for table in target_tables
        ]
        entered = False
        completed = False
        try:
            async with MySQLDatabase.shared_service_locks(
                names, timeout_seconds=self._lock_timeout
            ):
                entered = True
                yield
                completed = True
        except AdvisoryLockUnavailableError as error:
            if entered:
                raise
            raise DataAgentError(
                "generation_lock_unavailable",
                "query_readiness",
                "DW generation 正在变更，查询稍后可安全重试",
                retryable=True,
                http_status=409,
            ) from error
        except AdvisoryLockReleaseError:
            if not completed:
                raise
            logger.warning(
                "Query 已完成，但 generation READ lock owner 连接"
                "释放失败且已失效"
            )
