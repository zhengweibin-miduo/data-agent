"""回答前数据就绪状态的确定性路由。"""

from langchain_core.tools import BaseTool

from answer_readiness.classifier import AnswerReadinessClassifier
from answer_readiness.models import (
    AnswerGateDecision,
    AnswerGateResult,
    AnswerTargetCatalog,
    DataReadinessToolResult,
)
from answer_readiness.tool import create_data_readiness_tool

DATA_PREPARING_MESSAGE = "数据准备中，请稍后重试"
INTENT_UNRESOLVED_MESSAGE = "无法确定数据依赖，请重新表述问题"


class AnswerReadinessService:
    """组合意图识别和只读工具，返回确定性安全门禁。"""

    def __init__(
        self,
        classifier: AnswerReadinessClassifier | None = None,
        readiness_tool: BaseTool | None = None,
    ) -> None:
        """绑定复用型意图识别器和 LangChain 工具。"""
        self._classifier = classifier or AnswerReadinessClassifier()
        self._readiness_tool = readiness_tool or create_data_readiness_tool()

    async def evaluate(
        self,
        question: str,
        catalog: AnswerTargetCatalog,
    ) -> AnswerGateResult:
        """在任何业务回答前计算就绪门禁。"""
        # 步骤一：独立识别并校验意图；修复后仍无效时安全拒答。
        intent = await self._classifier.classify(question, catalog)
        if intent is None:
            return AnswerGateResult(
                decision=AnswerGateDecision.INTENT_UNRESOLVED,
                user_message=INTENT_UNRESOLVED_MESSAGE,
            )
        # 步骤二：不依赖 DW 的问题直接放行，且不访问 data_sync。
        if not intent.requires_sync_completion:
            return AnswerGateResult(decision=AnswerGateDecision.PROCEED)
        # 步骤三：所有依赖一次性交给只读工具，以 ready 作为唯一分支依据。
        raw_result = await self._readiness_tool.ainvoke(
            {
                "dependencies": [
                    dependency.model_dump(mode="json")
                    for dependency in intent.dependencies
                ]
            }
        )
        result = DataReadinessToolResult.model_validate(raw_result)
        if result.ready:
            return AnswerGateResult(decision=AnswerGateDecision.PROCEED)
        return AnswerGateResult(
            decision=AnswerGateDecision.DATA_PREPARING,
            user_message=DATA_PREPARING_MESSAGE,
        )
