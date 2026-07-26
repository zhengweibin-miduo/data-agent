"""DDL 任务请求、状态和结果契约。"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from data_agent.models.base import ContractModel
from data_agent.models.semantic import MetricAnswer, MetricQuestion


class JobStatus(StrEnum):
    """公开任务状态。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"


class JobEventType(StrEnum):
    """公开任务事件类型。"""

    SNAPSHOT = "snapshot"
    PROGRESS = "progress"
    WAITING_INPUT = "waiting_input"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    STREAM_ERROR = "stream_error"


class JobEventStage(StrEnum):
    """不泄漏 LangGraph 节点名的稳定业务阶段。"""

    QUEUED = "queued"
    RUNNING = "running"
    PARSING = "parsing"
    MEMORY_LOADING = "memory_loading"
    METADATA_GENERATING = "metadata_generating"
    METADATA_VALIDATING = "metadata_validating"
    QUESTION_PLANNING = "question_planning"
    WAITING_INPUT = "waiting_input"
    METRIC_GENERATING = "metric_generating"
    METRIC_VALIDATING = "metric_validating"
    MEMORY_BUILDING = "memory_building"
    PERSISTING = "persisting"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    STREAM_ERROR = "stream_error"


class JobError(ContractModel):
    """公开安全错误。"""

    code: str
    stage: str
    retryable: bool
    attempt: int = 0
    details: dict[str, str] = Field(default_factory=dict)


class JobResult(ContractModel):
    """成功任务结果摘要。"""

    ddl_hash: str
    table_count: int
    column_count: int
    metric_count: int


class JobRecord(ContractModel):
    """Redis 中公开任务投影。"""

    job_id: str
    source: str
    status: JobStatus
    revision: int = 0
    attempt: int = 0
    question_round: int = 0
    question_set_id: str | None = None
    questions: list[MetricQuestion] | None = None
    result: JobResult | None = None
    error: JobError | None = None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    graph_version: str


class JobEventData(ContractModel):
    """SSE 中只包含公开任务投影的类型化数据。"""

    job_id: str
    revision: int = Field(ge=0)
    attempt: int = Field(ge=0)
    status: JobStatus
    stage: JobEventStage
    emitted_at: datetime
    questions: list[MetricQuestion] | None = None
    result: JobResult | None = None
    error: JobError | None = None


class JobEvent(ContractModel):
    """带 Redis Stream 游标的公开任务事件。"""

    event_id: str
    event_type: JobEventType
    data: JobEventData


class DDLJobRequest(ContractModel):
    """DDL 任务提交请求。"""

    source: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+$")
    dialect: Literal["mysql"] = "mysql"
    ddl: str = Field(min_length=1)


class DDLJobAccepted(ContractModel):
    """DDL 任务受理响应。"""

    job_id: str
    status: Literal[JobStatus.PENDING] = JobStatus.PENDING
    status_url: str
    events_url: str | None = None


class AnswerRequest(ContractModel):
    """问题回答提交请求。"""

    revision: int = Field(ge=0)
    question_set_id: str
    answers: list[MetricAnswer] = Field(min_length=1, max_length=100)
