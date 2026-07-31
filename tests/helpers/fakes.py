"""LangGraph 工作流使用的确定性 fake 与测试数据。"""

from langchain_core.runnables import RunnableConfig

from data_agent.ddl_metadata.workflow.memory_context import LoadedMemoryContext
from data_agent.models.memory import (
    MemoryCandidate,
    MemoryContent,
)
from data_agent.models.physical import PhysicalSchema
from data_agent.models.semantic import (
    ColumnRole,
    ColumnValueIndexProfile,
    MetricAnswer,
    MetricMetadata,
    MetricOutput,
    MetricQuestion,
    MetricQuestionSet,
    SemanticColumn,
    SemanticMetadata,
    SemanticTable,
    TableRole,
    ValidationIssue,
    ValueIndexDecision,
    ValueSensitivity,
)

FACT_DDL = """
CREATE TABLE fact_order (
    order_id BIGINT PRIMARY KEY,
    amount DECIMAL(10,2)
)
"""
DIM_DDL = "CREATE TABLE dim_customer (id BIGINT PRIMARY KEY, name VARCHAR(50))"


def _config(thread_id: str) -> RunnableConfig:
    """构造 LangGraph 线程配置。"""
    return {"configurable": {"thread_id": thread_id}}


class _NoMemory:
    """不返回长期记忆的测试依赖。"""

    async def load(
        self,
        schema: PhysicalSchema,
    ) -> LoadedMemoryContext:
        return LoadedMemoryContext([], None, [], [], [])


class _CompleteMemory:
    """返回完整且兼容的语义与指标记忆。"""

    async def load(self, schema: PhysicalSchema) -> LoadedMemoryContext:
        builder = FakeMetadataGenerator()
        metadata = await builder.classify(schema, [], [])
        question_set = await builder.plan_questions(schema, metadata, [], 1)
        answers = [
            MetricAnswer(
                question_id=question_set.questions[0].question_id,
                answer="SUM(amount) / COUNT(order_id), yuan",
            )
        ]
        metric_output = await builder.generate_metrics(
            schema,
            metadata,
            question_set.questions,
            answers,
            [],
        )
        return LoadedMemoryContext(
            semantic_capsule=[],
            complete_semantic=metadata,
            questions=question_set.questions,
            answers=answers,
            metrics=metric_output.metrics,
        )


class _Snapshot:
    """记录持久化次数并可注入一次失败。"""

    def __init__(self, fail_once: bool = False) -> None:
        self.calls = 0
        self.fail_once = fail_once

    async def persist(
        self,
        schema: PhysicalSchema,
        metadata: SemanticMetadata,
        questions: list[MetricQuestion],
        answers: list[MetricAnswer],
        metrics: list[MetricMetadata],
        candidates: list[MemoryCandidate] | None = None,
    ) -> None:
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise ConnectionError("simulated MySQL disconnect")


class FakeMetadataGenerator:
    """基于输入 ID 生成确定性结构化响应。"""

    def __init__(
        self,
        hallucinate: bool = False,
        *,
        invalid_classify_once: bool = False,
        invalid_questions: bool = False,
        invalid_metric_once: bool = False,
        ambiguous_metrics: bool = False,
    ) -> None:
        """配置确定性输出或指定的单次故障。"""
        self.classify_calls = 0
        self.question_calls = 0
        self.metric_calls = 0
        self.hallucinate = hallucinate
        self.invalid_classify_once = invalid_classify_once
        self.invalid_questions = invalid_questions
        self.invalid_metric_once = invalid_metric_once
        self.ambiguous_metrics = ambiguous_metrics

    async def classify(
        self,
        schema: PhysicalSchema,
        issues: list[ValidationIssue],
        memory: list[MemoryContent],
    ) -> SemanticMetadata:
        """返回由输入稳定 ID 构造的语义分类。"""
        self.classify_calls += 1
        if self.invalid_classify_once and self.classify_calls == 1:
            raise ValueError("simulated invalid semantic structure")
        tables = [
            SemanticTable(
                table_id=("hallucinated" if self.hallucinate else table.id),
                role=(
                    TableRole.FACT if table.name.startswith("fact") else TableRole.DIM
                ),
                description=table.name,
                confidence=0.99,
                evidence=[table.id],
            )
            for table in schema.tables
        ]
        columns = [
            SemanticColumn(
                column_id=column.id,
                role=(
                    ColumnRole(column.structural_role)
                    if column.structural_role
                    else (
                        ColumnRole.MEASURE
                        if column.name == "amount"
                        else ColumnRole.DIMENSION
                    )
                ),
                description=column.name,
                confidence=0.99,
                evidence=[column.id],
                value_index=ColumnValueIndexProfile(
                    decision=ValueIndexDecision.INDEX,
                    sensitivity=ValueSensitivity.NON_SENSITIVE,
                    reason="测试字段可用于检索",
                    evidence=[table.id, column.id],
                ),
            )
            for table in schema.tables
            for column in table.columns
        ]
        return SemanticMetadata(tables=tables, columns=columns)

    async def plan_questions(
        self,
        schema: PhysicalSchema,
        metadata: SemanticMetadata,
        answers: list[MetricAnswer],
        round_number: int,
    ) -> MetricQuestionSet:
        """返回与事实表关联的确定性问题集。"""
        self.question_calls += 1
        if self.invalid_questions:
            raise ValueError("simulated invalid question structure")
        fact_id = next(
            table.table_id for table in metadata.tables if table.role == TableRole.FACT
        )
        amount_id = next(
            column.id
            for table in schema.tables
            for column in table.columns
            if column.name == "amount"
        )
        return MetricQuestionSet(
            questions=[
                MetricQuestion(
                    question_id="average_amount.definition",
                    prompt="Define average amount",
                    fact_table_id=fact_id,
                    column_ids=[amount_id],
                )
            ]
        )

    async def generate_metrics(
        self,
        schema: PhysicalSchema,
        metadata: SemanticMetadata,
        questions: list[MetricQuestion],
        answers: list[MetricAnswer],
        issues: list[ValidationIssue],
    ) -> MetricOutput:
        """返回由问题回答构造的确定性指标结果。"""
        self.metric_calls += 1
        if self.invalid_metric_once and self.metric_calls == 1:
            raise ValueError("simulated invalid metric structure")
        if self.ambiguous_metrics:
            return MetricOutput(
                metrics=[],
                missing_business_meaning=["missing calculation rule"],
            )
        return MetricOutput(
            metrics=[
                MetricMetadata(
                    name="average_amount",
                    fact_table_id=questions[0].fact_table_id,
                    definition=answers[0].answer,
                    relevant_column_ids=questions[0].column_ids,
                    answer_question_ids=[questions[0].question_id],
                )
            ]
        )
