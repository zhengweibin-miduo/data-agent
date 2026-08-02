"""LangGraph DDL 元数据检查点状态。"""

from typing import Literal, TypedDict


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
