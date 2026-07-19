"""DDL 元数据工作流依赖契约。"""

from dataclasses import dataclass
from typing import Protocol

from data_agent.ddl_metadata.memory.application.context import LoadedMemoryContext
from data_agent.ddl_metadata.models.memory import (
    MemoryCandidate,
    MemoryContent,
)
from data_agent.ddl_metadata.models.physical import PhysicalSchema
from data_agent.ddl_metadata.models.semantic import (
    MetricAnswer,
    MetricMetadata,
    MetricOutput,
    MetricQuestion,
    MetricQuestionSet,
    SemanticMetadata,
    ValidationIssue,
)


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


class MemoryContext(Protocol):
    """长期记忆读取节点的最小契约。"""

    async def load(
        self,
        schema: PhysicalSchema,
    ) -> LoadedMemoryContext:
        """加载与当前物理模式兼容的有界记忆上下文。"""
        ...


class SnapshotWriter(Protocol):
    """持久化节点的最小契约。"""

    async def persist(
        self,
        schema: PhysicalSchema,
        metadata: SemanticMetadata,
        questions: list[MetricQuestion],
        answers: list[MetricAnswer],
        metrics: list[MetricMetadata],
        candidates: list[MemoryCandidate] | None = None,
    ) -> None:
        """在一个事务中持久化已验证快照及候选记忆。"""
        ...


@dataclass(frozen=True)
class DDLGraphDependencies:
    """图节点的进程内依赖，不进入检查点。"""

    model: MetadataGenerator
    memory_context: MemoryContext
    snapshot: SnapshotWriter
