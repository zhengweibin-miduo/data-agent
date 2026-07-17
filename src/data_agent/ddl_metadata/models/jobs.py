"""DDL 任务请求、状态和结果契约。"""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field

from data_agent.ddl_metadata.models.base import ContractModel
from data_agent.ddl_metadata.models.semantic import MetricAnswer, MetricQuestion


class JobStatus(StrEnum):
    """公开任务状态。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"


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


class AnswerRequest(ContractModel):
    """问题回答提交请求。"""

    revision: int = Field(ge=0)
    question_set_id: str
    answers: list[MetricAnswer] = Field(min_length=1, max_length=100)
