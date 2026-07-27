"""LangChain 兼容的数据同步就绪检查工具。"""

from langchain_core.tools import StructuredTool

from data_agent.answer_readiness.models import (
    AnswerDataDependency,
    DataReadinessToolInput,
)
from data_agent.data_sync.models import SyncPhase
from data_agent.data_sync.repository import DataSyncRepository
from data_agent.infrastructure.mysql import MySQLDatabase

DATA_READINESS_TOOL_NAME = "check_dw_data_readiness"


def create_data_readiness_tool() -> StructuredTool:
    """创建只暴露 ready 布尔值的异步 LangChain 工具。"""

    async def check_dw_data_readiness(
        dependencies: list[AnswerDataDependency],
    ) -> dict[str, bool]:
        """检查回答依赖的全部 DW 同步任务是否已就绪。"""
        # 步骤一：每次工具调用使用新的托管 Session，只执行普通 SELECT。
        async with MySQLDatabase.session() as session:
            repository = DataSyncRepository(session)
            for dependency in dependencies:
                phases = await repository.read_readiness_phases(
                    target_table=dependency.target_table,
                    source=dependency.source,
                )
                # 步骤二：缺失、来源限定后匹配不唯一或任一非 streaming 均关闭门禁。
                if (
                    not phases
                    or (dependency.source is not None and len(phases) != 1)
                    or any(phase is not SyncPhase.STREAMING for phase in phases)
                ):
                    return {"ready": False}
        return {"ready": True}

    return StructuredTool.from_function(
        coroutine=check_dw_data_readiness,
        name=DATA_READINESS_TOOL_NAME,
        description=(
            "检查一个问题依赖的全部 DW 目标表是否已完成同步。"
            "工具仅返回 ready，不返回同步阶段、来源明细或进度。"
        ),
        args_schema=DataReadinessToolInput,
    )
