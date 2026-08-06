"""AST 实际目标表到既有 Answer Readiness 工具的适配器。"""

from langchain_core.tools import BaseTool

from answer_readiness.models import DataReadinessToolResult


class QueryReadinessAdapter:
    """直接检查最终目标表，避免模型再次改写已经解析的依赖。"""

    def __init__(self, tool: BaseTool) -> None:
        """绑定现有只读数据就绪工具。"""
        self._tool = tool

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
