"""可恢复的 LangGraph DDL 元数据工作流。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt
from loguru import logger

from data_agent.ddl_metadata.errors import DDLMetadataError
from data_agent.ddl_metadata.jobs.store import question_set_id
from data_agent.ddl_metadata.memory.context import LoadedMemoryContext
from data_agent.ddl_metadata.memory.snapshots import build_accepted_memories
from data_agent.ddl_metadata.models import (
    MEMORY_CONTENT_ADAPTER,
    JobError,
    JobResult,
    JobStatus,
    MemoryCandidate,
    MetricAnswer,
    MetricMetadata,
    MetricQuestion,
    PhysicalSchema,
    SemanticMetadata,
    TableRole,
    ValidationIssue,
)
from data_agent.ddl_metadata.parsing import parse_ddl
from data_agent.ddl_metadata.validation import (
    finalize_and_validate_metrics,
    validate_metadata,
    validate_metric_questions,
)
from data_agent.ddl_metadata.workflow.metadata_generator import MetadataGenerator


class DDLGraphState(TypedDict, total=False):
    """只包含检查点可序列化值的图状态。"""

    job_id: str
    source: str
    dialect: Literal["mysql"]
    ddl: str
    physical_schema: dict[str, object]
    semantic_metadata: dict[str, object]
    memory_capsule: list[dict[str, object]]
    reused_memory: list[dict[str, object]]
    metric_questions: list[dict[str, object]]
    current_questions: list[dict[str, object]]
    metric_answers: list[dict[str, object]]
    metrics: list[dict[str, object]]
    memory_candidates: list[dict[str, object]]
    validation_errors: list[dict[str, object]]
    semantic_attempts: int
    metric_attempts: int
    question_round: int
    status: str
    reused_metrics: bool
    route: str
    result: dict[str, object]
    error: dict[str, object]


@dataclass(frozen=True)
class DDLGraphDependencies:
    """图节点的进程内依赖，不进入检查点。"""

    model: MetadataGenerator
    memory_context: "MemoryContext"
    snapshot: "SnapshotWriter"


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


def _state_string(state: DDLGraphState, key: str) -> str:
    """读取图状态中的必需字符串。"""
    value = state.get(key)
    if not isinstance(value, str):
        raise ValueError(f"图状态缺少字符串字段 {key}")
    return value


def _error_update(error: DDLMetadataError) -> DDLGraphState:
    """把业务拒绝转为安全终态。"""
    return {
        "status": JobStatus.REJECTED.value,
        "route": "rejected",
        "error": JobError(
            code=error.code,
            stage=error.stage,
            retryable=error.retryable,
            details=error.details,
        ).model_dump(mode="json"),
    }


def build_ddl_metadata_graph(
    dependencies: DDLGraphDependencies,
    checkpointer: BaseCheckpointSaver | None = None,
) -> CompiledStateGraph:
    """构建串行、修订安全且可同步持久化的工作流。"""

    async def parse_node(state: DDLGraphState) -> DDLGraphState:
        logger.bind(
            trace_id=_state_string(state, "job_id"),
            component="ddl_metadata.workflow",
            event_name="ddl_metadata.workflow.node.started",
            operation="parse_ddl",
            outcome="started",
            node_name="parse_ddl",
        ).info("开始解析 DDL")
        try:
            schema = parse_ddl(
                _state_string(state, "source"),
                _state_string(state, "ddl"),
            )
        except DDLMetadataError as error:
            return _error_update(error)
        return {
            "physical_schema": schema.model_dump(mode="json"),
            "semantic_attempts": 0,
            "metric_attempts": 0,
            "question_round": 0,
            "metric_questions": [],
            "metric_answers": [],
            "validation_errors": [],
            "status": JobStatus.RUNNING.value,
            "route": "continue",
        }

    async def load_memory_node(state: DDLGraphState) -> DDLGraphState:
        logger.bind(
            trace_id=_state_string(state, "job_id"),
            component="ddl_metadata.workflow",
            event_name="ddl_metadata.workflow.node.started",
            operation="load_and_validate_memory",
            outcome="started",
            node_name="load_and_validate_memory",
        ).info("开始加载并校验记忆")
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        try:
            context = await dependencies.memory_context.load(schema)
        except DDLMetadataError as error:
            return _error_update(error)
        update: DDLGraphState = {
            "memory_capsule": [
                content.model_dump(mode="json") for content in context.semantic_capsule
            ],
            "reused_memory": [
                content.model_dump(mode="json") for content in context.reused_memory
            ],
            "metric_questions": [
                question.model_dump(mode="json") for question in context.questions
            ],
            "metric_answers": [
                answer.model_dump(mode="json") for answer in context.answers
            ],
            "metrics": [metric.model_dump(mode="json") for metric in context.metrics],
            "reused_metrics": bool(context.metrics),
            "route": "continue",
        }
        if context.complete_semantic is not None:
            update["semantic_metadata"] = context.complete_semantic.model_dump(
                mode="json"
            )
            update["route"] = "validate_cached"
        return update

    async def classify_node(state: DDLGraphState) -> DDLGraphState:
        logger.bind(
            trace_id=_state_string(state, "job_id"),
            component="ddl_metadata.workflow",
            event_name="ddl_metadata.workflow.node.started",
            operation="classify_metadata",
            outcome="started",
            node_name="classify_metadata",
            attempt=state.get("semantic_attempts", 0) + 1,
        ).info("开始分类元数据")
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        issues = [
            ValidationIssue.model_validate(issue)
            for issue in state.get("validation_errors", [])
        ]
        capsule = [
            MEMORY_CONTENT_ADAPTER.validate_python(value)
            for value in state.get("memory_capsule", [])
        ]
        try:
            metadata = await dependencies.model.classify(
                schema,
                issues,
                capsule,
            )
        except (TypeError, ValueError):
            attempts = state.get("semantic_attempts", 0)
            if attempts < 1:
                issue = ValidationIssue(
                    code="invalid_structured_output",
                    path="semantic_metadata",
                    message="模型语义响应未通过结构化解析",
                )
                return {
                    "validation_errors": [issue.model_dump(mode="json")],
                    "semantic_attempts": attempts + 1,
                    "route": "repair",
                }
            return _error_update(
                DDLMetadataError(
                    "invalid_semantic_output",
                    "classify_metadata",
                    "模型语义响应在修复后仍无法解析",
                )
            )
        return {
            "semantic_metadata": metadata.model_dump(mode="json"),
            "validation_errors": [],
            "route": "validate",
        }

    async def validate_metadata_node(state: DDLGraphState) -> DDLGraphState:
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        metadata = SemanticMetadata.model_validate(state.get("semantic_metadata"))
        issues = validate_metadata(schema, metadata)
        if not issues:
            return {"validation_errors": [], "route": "valid"}
        attempts = state.get("semantic_attempts", 0)
        if attempts < 1 and all(issue.repairable for issue in issues):
            return {
                "validation_errors": [
                    issue.model_dump(mode="json") for issue in issues
                ],
                "semantic_attempts": attempts + 1,
                "route": "repair",
            }
        error = DDLMetadataError(
            "invalid_semantic_metadata",
            "validate_metadata",
            "语义元数据未通过确定性校验",
            details={"codes": ",".join(sorted({issue.code for issue in issues}))},
        )
        return _error_update(error)

    async def plan_questions_node(state: DDLGraphState) -> DDLGraphState:
        logger.bind(
            trace_id=_state_string(state, "job_id"),
            component="ddl_metadata.workflow",
            event_name="ddl_metadata.workflow.node.started",
            operation="plan_metric_questions",
            outcome="started",
            node_name="plan_metric_questions",
            question_round=state.get("question_round", 0) + 1,
        ).info("开始规划指标问题")
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        metadata = SemanticMetadata.model_validate(state.get("semantic_metadata"))
        fact_ids = {
            table.table_id for table in metadata.tables if table.role == TableRole.FACT
        }
        if not fact_ids:
            return {
                "metrics": [],
                "current_questions": [],
                "route": "no_metrics",
            }
        round_number = state.get("question_round", 0) + 1
        answers = [
            MetricAnswer.model_validate(answer)
            for answer in state.get("metric_answers", [])
        ]
        try:
            output = await dependencies.model.plan_questions(
                schema,
                metadata,
                answers,
                round_number,
            )
        except (TypeError, ValueError):
            return _error_update(
                DDLMetadataError(
                    "invalid_metric_questions",
                    "plan_metric_questions",
                    "模型指标问题未通过结构化解析",
                )
            )
        issues = validate_metric_questions(
            schema,
            metadata,
            output.questions,
        )
        if issues or not output.questions:
            error = DDLMetadataError(
                "invalid_metric_questions",
                "plan_metric_questions",
                "事实表指标问题无效或为空",
                details={"codes": ",".join(sorted({issue.code for issue in issues}))},
            )
            return _error_update(error)
        history = [
            MetricQuestion.model_validate(question)
            for question in state.get("metric_questions", [])
        ]
        merged = {question.question_id: question for question in history}
        merged.update({question.question_id: question for question in output.questions})
        return {
            "metric_questions": [
                question.model_dump(mode="json") for question in merged.values()
            ],
            "current_questions": [
                question.model_dump(mode="json") for question in output.questions
            ],
            "question_round": round_number,
            "route": "await",
        }

    async def await_answers_node(state: DDLGraphState) -> DDLGraphState:
        current = [
            MetricQuestion.model_validate(question)
            for question in state.get("current_questions", [])
        ]
        resumed = interrupt(
            {
                "questions": [question.model_dump(mode="json") for question in current],
                "question_set_id": question_set_id(current),
                "question_round": state.get("question_round", 0),
            }
        )
        new_answers = [MetricAnswer.model_validate(answer) for answer in resumed]
        existing = {
            answer.question_id: answer
            for answer in (
                MetricAnswer.model_validate(answer)
                for answer in state.get("metric_answers", [])
            )
        }
        existing.update({answer.question_id: answer for answer in new_answers})
        return {
            "metric_answers": [
                answer.model_dump(mode="json") for answer in existing.values()
            ],
            "route": "generate",
        }

    async def generate_metrics_node(state: DDLGraphState) -> DDLGraphState:
        logger.bind(
            trace_id=_state_string(state, "job_id"),
            component="ddl_metadata.workflow",
            event_name="ddl_metadata.workflow.node.started",
            operation="generate_metrics",
            outcome="started",
            node_name="generate_metrics",
            attempt=state.get("metric_attempts", 0) + 1,
        ).info("开始生成指标")
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        metadata = SemanticMetadata.model_validate(state.get("semantic_metadata"))
        questions = [
            MetricQuestion.model_validate(question)
            for question in state.get("metric_questions", [])
        ]
        answers = [
            MetricAnswer.model_validate(answer)
            for answer in state.get("metric_answers", [])
        ]
        issues = [
            ValidationIssue.model_validate(issue)
            for issue in state.get("validation_errors", [])
        ]
        try:
            output = await dependencies.model.generate_metrics(
                schema,
                metadata,
                questions,
                answers,
                issues,
            )
        except (TypeError, ValueError):
            attempts = state.get("metric_attempts", 0)
            if attempts < 1:
                issue = ValidationIssue(
                    code="invalid_structured_output",
                    path="metrics",
                    message="模型指标响应未通过结构化解析",
                )
                return {
                    "validation_errors": [issue.model_dump(mode="json")],
                    "metric_attempts": attempts + 1,
                    "route": "repair",
                }
            return _error_update(
                DDLMetadataError(
                    "invalid_metric_output",
                    "generate_metrics",
                    "模型指标响应在修复后仍无法解析",
                )
            )
        if output.missing_business_meaning:
            if state.get("question_round", 0) < 2:
                return {
                    "validation_errors": [
                        ValidationIssue(
                            code="missing_business_meaning",
                            path="metrics",
                            message=value,
                            repairable=False,
                        ).model_dump(mode="json")
                        for value in output.missing_business_meaning
                    ],
                    "route": "followup",
                }
            return _error_update(
                DDLMetadataError(
                    "metric_ambiguity",
                    "generate_metrics",
                    "第二轮回答后指标定义仍不完整",
                )
            )
        return {
            "metrics": [metric.model_dump(mode="json") for metric in output.metrics],
            "validation_errors": [],
            "route": "validate",
        }

    async def validate_metrics_node(state: DDLGraphState) -> DDLGraphState:
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        metadata = SemanticMetadata.model_validate(state.get("semantic_metadata"))
        questions = [
            MetricQuestion.model_validate(question)
            for question in state.get("metric_questions", [])
        ]
        answers = [
            MetricAnswer.model_validate(answer)
            for answer in state.get("metric_answers", [])
        ]
        metrics = [
            MetricMetadata.model_validate(metric) for metric in state.get("metrics", [])
        ]
        finalized, issues = finalize_and_validate_metrics(
            _state_string(state, "source"),
            schema,
            metadata,
            questions,
            answers,
            metrics,
        )
        if not issues:
            return {
                "metrics": [metric.model_dump(mode="json") for metric in finalized],
                "validation_errors": [],
                "route": "valid",
            }
        attempts = state.get("metric_attempts", 0)
        if attempts < 1 and all(issue.repairable for issue in issues):
            return {
                "validation_errors": [
                    issue.model_dump(mode="json") for issue in issues
                ],
                "metric_attempts": attempts + 1,
                "route": "repair",
            }
        return _error_update(
            DDLMetadataError(
                "invalid_metrics",
                "validate_metrics",
                "指标未通过确定性校验",
                details={"codes": ",".join(sorted({issue.code for issue in issues}))},
            )
        )

    async def build_memories_node(state: DDLGraphState) -> DDLGraphState:
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        metadata = SemanticMetadata.model_validate(state.get("semantic_metadata"))
        questions = [
            MetricQuestion.model_validate(question)
            for question in state.get("metric_questions", [])
        ]
        answers = [
            MetricAnswer.model_validate(answer)
            for answer in state.get("metric_answers", [])
        ]
        metrics = [
            MetricMetadata.model_validate(metric) for metric in state.get("metrics", [])
        ]
        candidates = build_accepted_memories(
            schema,
            metadata,
            questions,
            answers,
            metrics,
            [
                MEMORY_CONTENT_ADAPTER.validate_python(content)
                for content in state.get("reused_memory", [])
            ],
        )
        return {
            "memory_candidates": [
                candidate.model_dump(mode="json") for candidate in candidates
            ],
            "route": "persist",
        }

    async def persist_node(state: DDLGraphState) -> DDLGraphState:
        logger.bind(
            trace_id=_state_string(state, "job_id"),
            component="ddl_metadata.workflow",
            event_name="ddl_metadata.workflow.node.started",
            operation="persist_snapshot",
            outcome="started",
            node_name="persist_snapshot",
        ).info("开始持久化快照")
        schema = PhysicalSchema.model_validate(state.get("physical_schema"))
        metadata = SemanticMetadata.model_validate(state.get("semantic_metadata"))
        questions = [
            MetricQuestion.model_validate(question)
            for question in state.get("metric_questions", [])
        ]
        answers = [
            MetricAnswer.model_validate(answer)
            for answer in state.get("metric_answers", [])
        ]
        metrics = [
            MetricMetadata.model_validate(metric) for metric in state.get("metrics", [])
        ]
        candidates = [
            MemoryCandidate.model_validate(candidate)
            for candidate in state.get("memory_candidates", [])
        ]
        await dependencies.snapshot.persist(
            schema,
            metadata,
            questions,
            answers,
            metrics,
            candidates,
        )
        result = JobResult(
            ddl_hash=schema.ddl_hash,
            table_count=len(schema.tables),
            column_count=sum(len(table.columns) for table in schema.tables),
            metric_count=len(metrics),
        )
        return {
            "status": JobStatus.SUCCEEDED.value,
            "result": result.model_dump(mode="json"),
            "route": "succeeded",
        }

    def after_terminal_guard(state: DDLGraphState) -> str:
        return END if state.get("route") == "rejected" else "load_and_validate_memory"

    def after_memory(state: DDLGraphState) -> str:
        if state.get("route") == "rejected":
            return END
        if state.get("route") == "validate_cached":
            return "validate_metadata"
        return "classify_metadata"

    def after_classification(state: DDLGraphState) -> str:
        return {
            "repair": "classify_metadata",
            "validate": "validate_metadata",
            "rejected": END,
        }[state.get("route", "rejected")]

    def after_metadata_validation(state: DDLGraphState) -> str:
        if state.get("route") == "valid" and state.get("reused_metrics"):
            return "validate_metrics"
        return {
            "repair": "classify_metadata",
            "valid": "plan_metric_questions",
            "rejected": END,
        }[state.get("route", "rejected")]

    def after_questions(state: DDLGraphState) -> str:
        return {
            "await": "await_metric_answers",
            "no_metrics": "build_memory_candidates",
            "rejected": END,
        }[state.get("route", "rejected")]

    def after_generation(state: DDLGraphState) -> str:
        return {
            "followup": "plan_metric_questions",
            "repair": "generate_metrics",
            "validate": "validate_metrics",
            "rejected": END,
        }[state.get("route", "rejected")]

    def after_metric_validation(state: DDLGraphState) -> str:
        return {
            "repair": "generate_metrics",
            "valid": "build_memory_candidates",
            "rejected": END,
        }[state.get("route", "rejected")]

    graph = StateGraph(DDLGraphState)
    graph.add_node("parse_ddl", parse_node)
    graph.add_node("load_and_validate_memory", load_memory_node)
    graph.add_node("classify_metadata", classify_node)
    graph.add_node("validate_metadata", validate_metadata_node)
    graph.add_node("plan_metric_questions", plan_questions_node)
    graph.add_node("await_metric_answers", await_answers_node)
    graph.add_node("generate_metrics", generate_metrics_node)
    graph.add_node("validate_metrics", validate_metrics_node)
    graph.add_node("build_memory_candidates", build_memories_node)
    graph.add_node("persist_snapshot", persist_node)
    graph.add_edge(START, "parse_ddl")
    graph.add_conditional_edges("parse_ddl", after_terminal_guard)
    graph.add_conditional_edges("load_and_validate_memory", after_memory)
    graph.add_conditional_edges(
        "classify_metadata",
        after_classification,
    )
    graph.add_conditional_edges(
        "validate_metadata",
        after_metadata_validation,
    )
    graph.add_conditional_edges(
        "plan_metric_questions",
        after_questions,
    )
    graph.add_edge("await_metric_answers", "generate_metrics")
    graph.add_conditional_edges("generate_metrics", after_generation)
    graph.add_conditional_edges(
        "validate_metrics",
        after_metric_validation,
    )
    graph.add_edge("build_memory_candidates", "persist_snapshot")
    graph.add_edge("persist_snapshot", END)
    return graph.compile(checkpointer=checkpointer)
