"""AST 实际目标表到既有 Answer Readiness 工具的适配器。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langchain_core.tools import BaseTool

from answer_readiness.models import DataReadinessToolResult
from data_sync.locks import generation_lock_name
from infrastructure.mysql import MySQLDatabase


class QueryReadinessAdapter:
    """直接检查最终目标表，避免模型再次改写已经解析的依赖。"""

    def __init__(
        self, tool: BaseTool, *, dw_database: str, lock_timeout: float
    ) -> None:
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
        """持有与同步重建共享的 generation locks。"""
        names = [
            generation_lock_name(self._dw_database, table) for table in target_tables
        ]
        async with MySQLDatabase.advisory_locks(
            names, timeout_seconds=self._lock_timeout
        ):
            yield
