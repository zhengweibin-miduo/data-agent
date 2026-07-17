"""LangGraph DDL 工作流及检查点恢复检查。"""

import asyncio

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.model.ddl_metadata import (
    ColumnRole,
    MemoryContent,
    MemoryCandidate,
    MetricAnswer,
    MetricMetadata,
    MetricOutput,
    MetricQuestion,
    MetricQuestionSet,
    PhysicalSchema,
    SemanticColumn,
    SemanticMetadata,
    SemanticTable,
    TableRole,
    ValidationIssue,
)
from app.service.ddl_metadata.graph import (
    DdlGraphDependencies,
    build_ddl_metadata_graph,
)
from app.service.ddl_metadata.memory_context import LoadedMemoryContext

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
        builder = FakeMetadataModel()
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


class FakeMetadataModel:
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
        self.classify_calls += 1
        if self.invalid_classify_once and self.classify_calls == 1:
            raise ValueError("simulated invalid semantic structure")
        tables = [
            SemanticTable(
                table_id=(
                    "hallucinated" if self.hallucinate else table.id
                ),
                role=(
                    TableRole.FACT
                    if table.name.startswith("fact")
                    else TableRole.DIM
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
        self.question_calls += 1
        if self.invalid_questions:
            raise ValueError("simulated invalid question structure")
        fact_id = next(
            table.table_id
            for table in metadata.tables
            if table.role == TableRole.FACT
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


async def _run_success_and_recovery() -> None:
    """验证 interrupt/resume 及持久化失败只重试持久化节点。"""
    model = FakeMetadataModel()
    snapshot = _Snapshot(fail_once=True)
    graph = build_ddl_metadata_graph(
        DdlGraphDependencies(model, _NoMemory(), snapshot),
        InMemorySaver(),
    )
    config = _config("graph-recovery")
    initial = {
        "job_id": "graph-recovery",
        "source": "test",
        "dialect": "mysql",
        "ddl": FACT_DDL,
    }
    await graph.ainvoke(initial, config, durability="sync")
    state = await graph.aget_state(config)
    assert state.next == ("await_metric_answers",)

    try:
        await graph.ainvoke(
            Command(
                resume=[
                    {
                        "question_id": "average_amount.definition",
                        "answer": "SUM(amount) / COUNT(order_id), yuan",
                    }
                ]
            ),
            config,
            durability="sync",
        )
    except ConnectionError:
        pass
    else:
        raise AssertionError("第一次持久化必须注入失败")
    counts = (
        model.classify_calls,
        model.question_calls,
        model.metric_calls,
    )
    result = await graph.ainvoke(None, config, durability="sync")
    assert result["status"] == "succeeded"
    assert snapshot.calls == 2
    assert counts == (
        model.classify_calls,
        model.question_calls,
        model.metric_calls,
    )


async def _run_rejection_and_dimension_path() -> None:
    """验证解析前拒绝不调模型、幻觉修复耗尽和维表无指标路径。"""
    model = FakeMetadataModel()
    snapshot = _Snapshot()
    graph = build_ddl_metadata_graph(
        DdlGraphDependencies(model, _NoMemory(), snapshot),
        InMemorySaver(),
    )
    rejected = await graph.ainvoke(
        {
            "job_id": "parse-rejected",
            "source": "test",
            "dialect": "mysql",
            "ddl": "DROP TABLE x",
        },
        _config("parse-rejected"),
        durability="sync",
    )
    assert rejected["status"] == "rejected"
    assert model.classify_calls == 0

    hallucinating = FakeMetadataModel(hallucinate=True)
    graph = build_ddl_metadata_graph(
        DdlGraphDependencies(hallucinating, _NoMemory(), _Snapshot()),
        InMemorySaver(),
    )
    rejected = await graph.ainvoke(
        {
            "job_id": "hallucination",
            "source": "test",
            "dialect": "mysql",
            "ddl": DIM_DDL,
        },
        _config("hallucination"),
        durability="sync",
    )
    assert rejected["status"] == "rejected"
    assert hallucinating.classify_calls == 2

    model = FakeMetadataModel()
    snapshot = _Snapshot()
    graph = build_ddl_metadata_graph(
        DdlGraphDependencies(model, _NoMemory(), snapshot),
        InMemorySaver(),
    )
    result = await graph.ainvoke(
        {
            "job_id": "dimension",
            "source": "test",
            "dialect": "mysql",
            "ddl": DIM_DDL,
        },
        _config("dimension"),
        durability="sync",
    )
    assert result["status"] == "succeeded"
    assert model.question_calls == 0
    assert model.metric_calls == 0
    assert snapshot.calls == 1

    reused_model = FakeMetadataModel()
    reused_snapshot = _Snapshot()
    graph = build_ddl_metadata_graph(
        DdlGraphDependencies(
            reused_model,
            _CompleteMemory(),
            reused_snapshot,
        ),
        InMemorySaver(),
    )
    reused = await graph.ainvoke(
        {
            "job_id": "memory-reuse",
            "source": "test",
            "dialect": "mysql",
            "ddl": FACT_DDL,
        },
        _config("memory-reuse"),
        durability="sync",
    )
    assert reused["status"] == "succeeded"
    assert reused_model.classify_calls == 0
    assert reused_model.question_calls == 0
    assert reused_model.metric_calls == 0
    assert reused_snapshot.calls == 1


async def _run_structured_output_repair() -> None:
    """验证结构化解析失败只修复一次并留在图内。"""
    classify_model = FakeMetadataModel(invalid_classify_once=True)
    graph = build_ddl_metadata_graph(
        DdlGraphDependencies(
            classify_model,
            _NoMemory(),
            _Snapshot(),
        ),
        InMemorySaver(),
    )
    result = await graph.ainvoke(
        {
            "job_id": "classify-structure-repair",
            "source": "test",
            "dialect": "mysql",
            "ddl": DIM_DDL,
        },
        _config("classify-structure-repair"),
        durability="sync",
    )
    assert result["status"] == "succeeded"
    assert classify_model.classify_calls == 2

    question_model = FakeMetadataModel(invalid_questions=True)
    graph = build_ddl_metadata_graph(
        DdlGraphDependencies(
            question_model,
            _NoMemory(),
            _Snapshot(),
        ),
        InMemorySaver(),
    )
    result = await graph.ainvoke(
        {
            "job_id": "question-structure-rejected",
            "source": "test",
            "dialect": "mysql",
            "ddl": FACT_DDL,
        },
        _config("question-structure-rejected"),
        durability="sync",
    )
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "invalid_metric_questions"

    metric_model = FakeMetadataModel(invalid_metric_once=True)
    graph = build_ddl_metadata_graph(
        DdlGraphDependencies(
            metric_model,
            _NoMemory(),
            _Snapshot(),
        ),
        InMemorySaver(),
    )
    config = _config("metric-structure-repair")
    await graph.ainvoke(
        {
            "job_id": "metric-structure-repair",
            "source": "test",
            "dialect": "mysql",
            "ddl": FACT_DDL,
        },
        config,
        durability="sync",
    )
    result = await graph.ainvoke(
        Command(
            resume=[
                {
                    "question_id": "average_amount.definition",
                    "answer": "SUM(amount) / COUNT(order_id), yuan",
                }
            ]
        ),
        config,
        durability="sync",
    )
    assert result["status"] == "succeeded"
    assert metric_model.metric_calls == 2


async def _run_two_round_ambiguity() -> None:
    """验证第二轮仍不完整时拒绝且不进入持久化。"""
    model = FakeMetadataModel(ambiguous_metrics=True)
    snapshot = _Snapshot()
    graph = build_ddl_metadata_graph(
        DdlGraphDependencies(model, _NoMemory(), snapshot),
        InMemorySaver(),
    )
    config = _config("two-round-ambiguity")
    await graph.ainvoke(
        {
            "job_id": "two-round-ambiguity",
            "source": "test",
            "dialect": "mysql",
            "ddl": FACT_DDL,
        },
        config,
        durability="sync",
    )
    answer = Command(
        resume=[
            {
                "question_id": "average_amount.definition",
                "answer": "Still incomplete",
            }
        ]
    )
    await graph.ainvoke(answer, config, durability="sync")
    state = await graph.aget_state(config)
    assert state.next == ("await_metric_answers",)
    result = await graph.ainvoke(answer, config, durability="sync")
    assert result["status"] == "rejected"
    assert result["error"]["code"] == "metric_ambiguity"
    assert snapshot.calls == 0


def test_ddl_metadata_graph() -> None:
    """运行确定性图检查。"""
    asyncio.run(_run_success_and_recovery())
    asyncio.run(_run_rejection_and_dimension_path())
    asyncio.run(_run_structured_output_repair())
    asyncio.run(_run_two_round_ambiguity())


if __name__ == "__main__":
    test_ddl_metadata_graph()
