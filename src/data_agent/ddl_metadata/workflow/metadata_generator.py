"""DDL 元数据结构化模型调用。"""

import asyncio
import json
from typing import Protocol, TypeVar, cast

from langchain_openai import ChatOpenAI
from pydantic import BaseModel

from data_agent.ddl_metadata.models import (
    MemoryContent,
    MetricAnswer,
    MetricOutput,
    MetricQuestion,
    MetricQuestionSet,
    PhysicalSchema,
    SemanticMetadata,
    ValidationIssue,
)
from data_agent.infrastructure.llm_client import LLMClient
from data_agent.settings import app_config

OutputT = TypeVar("OutputT", bound=BaseModel)


class MetadataGenerator(Protocol):
    """图节点依赖的最小结构化模型契约。"""

    async def classify(
        self,
        schema: PhysicalSchema,
        issues: list[ValidationIssue],
        memory: list[MemoryContent],
    ) -> SemanticMetadata:
        """生成表列语义分类。"""
        ...

    async def plan_questions(
        self,
        schema: PhysicalSchema,
        metadata: SemanticMetadata,
        answers: list[MetricAnswer],
        round_number: int,
    ) -> MetricQuestionSet:
        """生成当前轮次所需的指标澄清问题。"""
        ...

    async def generate_metrics(
        self,
        schema: PhysicalSchema,
        metadata: SemanticMetadata,
        questions: list[MetricQuestion],
        answers: list[MetricAnswer],
        issues: list[ValidationIssue],
    ) -> MetricOutput:
        """根据已验证语义和用户回答生成指标。"""
        ...


class LLMMetadataGenerator:
    """基于 ChatOpenAI Pydantic 结构化输出的生产实现。"""

    def __init__(self, client: ChatOpenAI | None = None) -> None:
        """绑定结构化输出客户端并建立并发限制。"""
        self._client = client or LLMClient.get_client()
        self._semaphore = asyncio.Semaphore(app_config.llm.max_concurrency)

    async def _invoke(
        self,
        output_type: type[OutputT],
        system: str,
        payload: dict[str, object],
    ) -> OutputT:
        """调用配置的结构化输出方法，禁止文本降级。"""
        runnable = self._client.with_structured_output(
            output_type,
            method=app_config.llm.structured_output_method,
        )
        async with self._semaphore:
            value = await runnable.ainvoke(
                [
                    ("system", system),
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
        if not isinstance(value, output_type):
            raise TypeError(f"模型未返回 {output_type.__name__}")
        return cast(OutputT, value)

    async def classify(
        self,
        schema: PhysicalSchema,
        issues: list[ValidationIssue],
        memory: list[MemoryContent],
    ) -> SemanticMetadata:
        """分类表列语义，不允许改变物理对象。"""
        return await self._invoke(
            SemanticMetadata,
            (
                "Classify every supplied table as fact or dim and every supplied "
                "column using only its supplied ID. Preserve primary_key and "
                "foreign_key structural roles. Other columns are measure or "
                "dimension. Return every object exactly once. Evidence entries "
                "must be supplied table/column IDs. Never invent objects. "
                "Descriptions are concise accepted facts, not hidden reasoning."
            ),
            {
                "physical_schema": schema.model_dump(
                    mode="json",
                    exclude={"canonical_ddl"},
                ),
                "compatible_memory": [item.model_dump(mode="json") for item in memory],
                "validation_feedback": [
                    issue.model_dump(mode="json") for issue in issues
                ],
            },
        )

    async def plan_questions(
        self,
        schema: PhysicalSchema,
        metadata: SemanticMetadata,
        answers: list[MetricAnswer],
        round_number: int,
    ) -> MetricQuestionSet:
        """只询问事实表指标仍缺失的明确业务定义。"""
        return await self._invoke(
            MetricQuestionSet,
            (
                "Ask only questions required to define business metrics for "
                "fact tables. Use stable question IDs and only supplied table/"
                "column IDs. Ask calculation, filters, time grain and unit when "
                "applicable. Do not ask for facts already explicit in answers. "
                "Return no questions only when no metric is required."
            ),
            {
                "physical_schema": schema.model_dump(
                    mode="json",
                    exclude={"canonical_ddl"},
                ),
                "semantic_metadata": metadata.model_dump(mode="json"),
                "previous_answers": [
                    answer.model_dump(mode="json") for answer in answers
                ],
                "round_number": round_number,
            },
        )

    async def generate_metrics(
        self,
        schema: PhysicalSchema,
        metadata: SemanticMetadata,
        questions: list[MetricQuestion],
        answers: list[MetricAnswer],
        issues: list[ValidationIssue],
    ) -> MetricOutput:
        """仅从校验对象和明确回答生成指标。"""
        return await self._invoke(
            MetricOutput,
            (
                "Generate metrics only from supplied validated metadata and "
                "answers. Each metric must cite answer question IDs and current "
                "column IDs. Include calculation, filters, time grain and unit "
                "in definition when applicable. Put still-missing business "
                "facts in missing_business_meaning; never guess them."
            ),
            {
                "physical_schema": schema.model_dump(
                    mode="json",
                    exclude={"canonical_ddl"},
                ),
                "semantic_metadata": metadata.model_dump(mode="json"),
                "questions": [
                    question.model_dump(mode="json") for question in questions
                ],
                "answers": [answer.model_dump(mode="json") for answer in answers],
                "validation_feedback": [
                    issue.model_dump(mode="json") for issue in issues
                ],
            },
        )
