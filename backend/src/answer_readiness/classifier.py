"""回答数据依赖的结构化 LLM 意图识别。"""

import asyncio
import json
from typing import cast

from langchain_openai import ChatOpenAI

from answer_readiness.models import (
    AnswerReadinessIntent,
    AnswerTargetCatalog,
)
from infrastructure.llm_client import LLMClient
from settings import app_config

_SYSTEM_PROMPT = (
    "Classify whether the user's question requires complete DW data before it can "
    "be answered. Use only target_table and source values from the supplied "
    "catalog. Set source only when the question explicitly limits that source; "
    "otherwise use null so all sources for the target are checked. "
    "requires_sync_completion=false requires an empty dependencies list; true "
    "requires at least one dependency. Return each target at most once. Keep reason "
    "concise and do not include hidden reasoning."
)


class AnswerReadinessClassifier:
    """使用现有 LLM 客户端执行独立、可修复一次的意图识别。"""

    def __init__(self, client: ChatOpenAI | None = None) -> None:
        """绑定托管模型客户端和现有并发预算。"""
        self._client = client or LLMClient.get_client()
        self._semaphore = asyncio.Semaphore(app_config.llm.max_concurrency)

    async def classify(
        self,
        question: str,
        catalog: AnswerTargetCatalog,
    ) -> AnswerReadinessIntent | None:
        """识别问题依赖；首次无效时修复一次，持续无效则返回空。"""
        if not question or len(question) > 8000:
            return None
        for attempt in range(2):
            try:
                intent = await self._invoke(
                    question,
                    catalog,
                    repair=attempt == 1,
                )
                intent.validate_catalog(catalog)
            except (TypeError, ValueError):
                continue
            return intent
        return None

    async def _invoke(
        self,
        question: str,
        catalog: AnswerTargetCatalog,
        *,
        repair: bool,
    ) -> AnswerReadinessIntent:
        """执行一次严格结构化调用，不回退到文本解析。"""
        # 步骤一：绑定回答意图契约，沿用全局配置的结构化输出方法。
        runnable = self._client.with_structured_output(
            AnswerReadinessIntent,
            method=app_config.llm.structured_output_method,
        )
        payload: dict[str, object] = {
            "question": question,
            "target_catalog": catalog.model_dump(mode="json"),
        }
        if repair:
            payload["validation_feedback"] = (
                "The previous response violated the typed contract or target catalog. "
                "Return one corrected structured response."
            )
        # 步骤二：只发送有界问题、业务目录和固定修复提示，不包含同步控制状态。
        async with self._semaphore:
            value = await runnable.ainvoke(
                [
                    ("system", _SYSTEM_PROMPT),
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
        # 步骤三：拒绝宽松字典或文本结果，由外层消耗唯一修复预算。
        if not isinstance(value, AnswerReadinessIntent):
            raise TypeError("模型未返回 AnswerReadinessIntent")
        return cast(AnswerReadinessIntent, value)
