"""查询意图、草稿和唯一修复的严格结构化 LLM 适配器。"""

import asyncio
import json
from typing import TypeVar, cast

from langchain_core.language_models.chat_models import BaseChatModel
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAIError,
    RateLimitError,
)

from errors import DataAgentError
from models.base import ContractModel
from query.domain import (
    QueryContext,
    QueryDraft,
    QueryIntent,
    SQLValidationIssue,
)
from settings import app_config

_Output = TypeVar("_Output", bound=ContractModel)
_RETRYABLE_MODEL_ERRORS = (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
_INTENT_PROMPT = """Extract one strict QueryIntent from the supplied user messages.
Every quote field must be an exact non-empty substring of a supplied user message.
Do not invent database object IDs, implicit metrics, filters, time ranges,
limits, or defaults.
Record unresolved meaning in ambiguities; do not resolve it with confidence.
Return only the typed structured result and no hidden reasoning."""
_DRAFT_PROMPT = """Generate one MySQL QueryDraft from the authoritative QueryContext.
Use only dw tables and columns in the supplied physical schema and bindings.
Return one SELECT or WITH ... SELECT, named placeholders for user values, and exact
referenced table/column/metric IDs. Do not use SELECT star, comments, user variables,
dangerous functions, file output, unsupported joins, or a LIMIT the QueryIntent did not
explicitly request. Return only the typed structured result and no hidden reasoning."""


class QueryLLMAdapter:
    """复用共享零温度模型与并发预算的查询结构化适配器。"""

    def __init__(self, model: BaseChatModel) -> None:
        """绑定已经由组合根初始化的共享模型。"""
        self._model = model
        self._semaphore = asyncio.Semaphore(app_config.llm.max_concurrency)

    async def parse(self, question: str, user_messages: list[str]) -> QueryIntent:
        """提取严格 QueryIntent；契约错误仅修复一次并最终失败关闭。"""
        payload: dict[str, object] = {
            "current_question": question,
            "user_messages": user_messages,
        }
        for attempt in range(2):
            if attempt:
                payload["validation_feedback"] = (
                    "Previous output violated the typed contract or exact quote "
                    "evidence. "
                    "Return one corrected QueryIntent."
                )
            try:
                intent = await self._invoke(QueryIntent, _INTENT_PROMPT, payload)
                intent.validate_evidence(user_messages)
            except (TypeError, ValueError):
                continue
            return intent
        raise DataAgentError(
            "query_intent_invalid",
            "query_intent",
            "查询意图两次未通过严格原文证据校验",
            http_status=422,
        )

    async def draft(self, context: QueryContext, intent: QueryIntent) -> QueryDraft:
        """从有界权威上下文生成首个不可直接执行的 QueryDraft。"""
        return await self._invoke(
            QueryDraft,
            _DRAFT_PROMPT,
            {
                "intent": intent.model_dump(mode="json"),
                "context": context.model_dump(mode="json"),
            },
        )

    async def repair(
        self,
        context: QueryContext,
        intent: QueryIntent,
        draft: QueryDraft,
        issues: tuple[SQLValidationIssue, ...],
    ) -> QueryDraft:
        """仅以稳定问题代码和对象名修复一次 SQL 草稿。"""
        return await self._invoke(
            QueryDraft,
            _DRAFT_PROMPT,
            {
                "intent": intent.model_dump(mode="json"),
                "context": context.model_dump(mode="json"),
                "previous_draft": draft.model_dump(mode="json"),
                "validation_feedback": [
                    issue.model_dump(mode="json") for issue in issues
                ],
                "repair_budget_remaining": 0,
            },
        )

    async def _invoke(
        self,
        output_type: type[_Output],
        system_prompt: str,
        payload: dict[str, object],
    ) -> _Output:
        """执行一次严格 structured output，不回退到文本或宽松字典。"""
        runnable = self._model.with_structured_output(
            output_type,
            method=app_config.llm.structured_output_method,
        )
        try:
            async with self._semaphore:
                value = await runnable.ainvoke(
                    [
                        ("system", system_prompt),
                        (
                            "user",
                            json.dumps(
                                payload,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        ),
                    ]
                )
        except OpenAIError as error:
            raise DataAgentError(
                "query_model_failed",
                "query_model",
                "查询模型调用失败",
                retryable=isinstance(error, _RETRYABLE_MODEL_ERRORS),
                http_status=502,
            ) from error
        if not isinstance(value, output_type):
            raise TypeError(f"模型未返回 {output_type.__name__}")
        return cast(_Output, value)
