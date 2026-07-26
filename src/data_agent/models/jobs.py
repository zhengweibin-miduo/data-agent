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

    code: str = Field(description="代码或错误代码。")
    stage: str = Field(description="业务阶段。")
    retryable: bool = Field(description="错误是否可重试。")
    attempt: int = Field(default=0, description="处理尝试次数。")
    details: dict[str, str] = Field(default_factory=dict, description="附加详情。")


class JobResult(ContractModel):
    """成功任务结果摘要。"""

    ddl_hash: str = Field(description="DDL 内容哈希。")
    table_count: int = Field(description="表数量。")
    column_count: int = Field(description="列数量。")
    metric_count: int = Field(description="指标数量。")


class JobRecord(ContractModel):
    """Redis 中公开任务投影。"""

    job_id: str = Field(description="任务唯一标识。")
    source: str = Field(description="数据来源标识。")
    status: JobStatus = Field(description="当前状态。")
    revision: int = Field(default=0, description="对象修订号。")
    attempt: int = Field(default=0, description="处理尝试次数。")
    question_round: int = Field(default=0, description="当前澄清问题轮次。")
    question_set_id: str | None = Field(default=None, description="问题集标识。")
    questions: list[MetricQuestion] | None = Field(
        default=None, description="问题列表。"
    )
    result: JobResult | None = Field(default=None, description="处理结果。")
    error: JobError | None = Field(default=None, description="错误信息。")
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="最近更新时间。")
    expires_at: datetime | None = Field(default=None, description="过期时间。")
    # graph_version 是 worker 判断"任务是否由当前图版本继续解释"的内部兼容字段，
    # 调用方无从使用，因此不进入公开投影；worker 通过内部读取路径获取。


class JobEventData(ContractModel):
    """SSE 中只包含公开任务投影的类型化数据。"""

    job_id: str = Field(description="任务唯一标识。")
    revision: int = Field(ge=0, description="对象修订号。")
    attempt: int = Field(ge=0, description="处理尝试次数。")
    status: JobStatus = Field(description="当前状态。")
    stage: JobEventStage = Field(description="业务阶段。")
    emitted_at: datetime = Field(description="事件发出时间。")
    questions: list[MetricQuestion] | None = Field(
        default=None, description="问题列表。"
    )
    result: JobResult | None = Field(default=None, description="处理结果。")
    error: JobError | None = Field(default=None, description="错误信息。")


class JobEvent(ContractModel):
    """带 Redis Stream 游标的公开任务事件。"""

    event_id: str = Field(description="事件唯一标识。")
    event_type: JobEventType = Field(description="事件类型。")
    data: JobEventData = Field(description="扩展数据。")


class DDLJobRequest(ContractModel):
    """DDL 任务提交请求。"""

    source: str = Field(
        min_length=1, max_length=128, pattern=r"^[\w.-]+$", description="数据来源标识。"
    )
    dialect: Literal["mysql"] = Field(default="mysql", description="DDL 方言。")
    ddl: str = Field(min_length=1, description="DDL 文本。")


class DDLJobAccepted(ContractModel):
    """DDL 任务受理响应。"""

    job_id: str = Field(description="任务唯一标识。")
    status: Literal[JobStatus.PENDING] = Field(
        default=JobStatus.PENDING, description="当前状态。"
    )
    status_url: str = Field(description="任务状态查询地址。")
    events_url: str | None = Field(default=None, description="任务事件流地址。")


class AnswerRequest(ContractModel):
    """问题回答提交请求。"""

    revision: int = Field(ge=0, description="对象修订号。")
    question_set_id: str = Field(description="问题集标识。")
    answers: list[MetricAnswer] = Field(
        min_length=1, max_length=100, description="回答列表。"
    )
