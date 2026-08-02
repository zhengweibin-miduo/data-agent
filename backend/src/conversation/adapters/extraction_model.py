"""OpenAI-compatible structured Conversation extraction adapter。"""

import json

from langchain_core.language_models.base import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel

from conversation.models import ClaimedExtraction, ExtractionResult

_SYSTEM_PROMPT = """你只提炼用户明确表达的身份、偏好、约束和业务规则。
不要记录助手建议、猜测、推断、模糊确认、Prompt、凭据或隐藏推理。
每个候选必须给出用户消息中的精确原文 supporting_user_quote 和消息 UID。
若事实来自助手结论，必须同时给出助手消息 UID、助手精确原文和后续用户明确确认原文。
返回更新后的有界摘要和零到多条候选。"""


class StructuredExtractionModel:
    """把共享聊天模型适配为 Conversation extraction port。"""

    def __init__(self, model: BaseChatModel, *, method: str) -> None:
        """绑定共享模型和结构化输出方法。"""
        self._structured: Runnable[
            LanguageModelInput, dict[str, object] | BaseModel
        ] = model.with_structured_output(ExtractionResult, method=method)

    async def extract(self, claim: ClaimedExtraction) -> ExtractionResult:
        """在无数据库事务环境中生成结构化摘要与候选。"""
        payload = {
            "summary": claim.summary,
            "messages": [
                {
                    "uid": message.uid,
                    "id": message.id,
                    "role": message.role.value,
                    "content": message.content,
                }
                for message in claim.messages
            ],
        }
        value = await self._structured.ainvoke(
            [
                SystemMessage(content=_SYSTEM_PROMPT),
                HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
            ]
        )
        return ExtractionResult.model_validate(value)
