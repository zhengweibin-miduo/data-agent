"""LangGraph DDL 工作流及检查点恢复检查。"""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from data_agent.ddl_metadata.workflow.graph import (
    DDLGraphDependencies,
    build_ddl_metadata_graph,
)
from tests.helpers.fakes import (
    DIM_DDL,
    FACT_DDL,
    FakeMetadataGenerator,
    _CompleteMemory,
    _config,
    _NoMemory,
    _Snapshot,
)


async def _run_success_and_recovery() -> None:
    """验证 interrupt/resume 及持久化失败只重试持久化节点。"""
    model = FakeMetadataGenerator()
    snapshot = _Snapshot(fail_once=True)
    graph = build_ddl_metadata_graph(
        DDLGraphDependencies(model, _NoMemory(), snapshot),
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
    model = FakeMetadataGenerator()
    snapshot = _Snapshot()
    graph = build_ddl_metadata_graph(
        DDLGraphDependencies(model, _NoMemory(), snapshot),
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

    hallucinating = FakeMetadataGenerator(hallucinate=True)
    graph = build_ddl_metadata_graph(
        DDLGraphDependencies(hallucinating, _NoMemory(), _Snapshot()),
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

    model = FakeMetadataGenerator()
    snapshot = _Snapshot()
    graph = build_ddl_metadata_graph(
        DDLGraphDependencies(model, _NoMemory(), snapshot),
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

    reused_model = FakeMetadataGenerator()
    reused_snapshot = _Snapshot()
    graph = build_ddl_metadata_graph(
        DDLGraphDependencies(
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
    classify_model = FakeMetadataGenerator(invalid_classify_once=True)
    graph = build_ddl_metadata_graph(
        DDLGraphDependencies(
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

    question_model = FakeMetadataGenerator(invalid_questions=True)
    graph = build_ddl_metadata_graph(
        DDLGraphDependencies(
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

    metric_model = FakeMetadataGenerator(invalid_metric_once=True)
    graph = build_ddl_metadata_graph(
        DDLGraphDependencies(
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
    model = FakeMetadataGenerator(ambiguous_metrics=True)
    snapshot = _Snapshot()
    graph = build_ddl_metadata_graph(
        DDLGraphDependencies(model, _NoMemory(), snapshot),
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


async def test_ddl_metadata_graph() -> None:
    """运行确定性图检查。"""
    await _run_success_and_recovery()
    await _run_rejection_and_dimension_path()
    await _run_structured_output_repair()
    await _run_two_round_ambiguity()
