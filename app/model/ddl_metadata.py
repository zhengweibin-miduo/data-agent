"""DDL 元数据工作流的共享类型契约。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)


class ContractModel(BaseModel):
    """严格且可安全序列化的业务契约基类。"""

    model_config = ConfigDict(extra="forbid")


class JobStatus(StrEnum):
    """公开任务状态。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"


class TableRole(StrEnum):
    """表语义角色。"""

    FACT = "fact"
    DIM = "dim"


class ColumnRole(StrEnum):
    """列角色。"""

    PRIMARY_KEY = "primary_key"
    FOREIGN_KEY = "foreign_key"
    MEASURE = "measure"
    DIMENSION = "dimension"


class MemoryKind(StrEnum):
    """长期记忆类型。"""

    SEMANTIC_DECISION = "SEMANTIC_DECISION"
    METRIC_QUESTION = "METRIC_QUESTION"
    USER_ANSWER = "USER_ANSWER"
    METRIC_DEFINITION = "METRIC_DEFINITION"


class MemoryRowStatus(StrEnum):
    """长期记忆行状态。"""

    NORMAL = "NORMAL"
    ARCHIVED = "ARCHIVED"


class MemoryRelationType(StrEnum):
    """长期记忆关系类型。"""

    REFERENCE = "REFERENCE"
    COMMENT = "COMMENT"
    SUPERSEDES = "SUPERSEDES"


class PhysicalColumn(ContractModel):
    """由 DDL AST 唯一确定的列信息。"""

    id: str
    name: str
    data_type: str
    comment: str | None = None
    structural_role: Literal["primary_key", "foreign_key"] | None = None


class PhysicalTable(ContractModel):
    """由 DDL AST 唯一确定的表信息。"""

    id: str
    schema_name: str | None = None
    name: str
    qualified_name: str
    comment: str | None = None
    columns: list[PhysicalColumn]


class PhysicalSchema(ContractModel):
    """规范化后的完整物理模式。"""

    source: str
    canonical_ddl: str
    ddl_hash: str
    schema_fingerprint: str
    tables: list[PhysicalTable]


class SemanticTable(ContractModel):
    """模型返回并待确定性校验的表语义。"""

    table_id: str
    role: TableRole
    description: str = Field(min_length=1, max_length=4000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=20)


class SemanticColumn(ContractModel):
    """模型返回并待确定性校验的列语义。"""

    column_id: str
    role: ColumnRole
    description: str = Field(min_length=1, max_length=4000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=20)


class SemanticMetadata(ContractModel):
    """结构化表列语义响应。"""

    tables: list[SemanticTable]
    columns: list[SemanticColumn]


class MetricQuestion(ContractModel):
    """一次指标澄清问题。"""

    question_id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=2000)
    fact_table_id: str
    column_ids: list[str] = Field(default_factory=list, max_length=50)
    required: bool = True


class MetricQuestionSet(ContractModel):
    """模型生成的一轮指标问题。"""

    questions: list[MetricQuestion] = Field(max_length=100)


class MetricAnswer(ContractModel):
    """用户对单个指标问题的回答。"""

    question_id: str
    answer: str = Field(min_length=1, max_length=8000)


class MetricMetadata(ContractModel):
    """最终校验通过的指标元数据。"""

    id: str = ""
    name: str = Field(min_length=1, max_length=128)
    fact_table_id: str
    definition: str = Field(min_length=1, max_length=8000)
    relevant_column_ids: list[str] = Field(min_length=1, max_length=100)
    answer_question_ids: list[str] = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list, max_length=20)


class MetricOutput(ContractModel):
    """结构化指标响应。"""

    metrics: list[MetricMetadata] = Field(max_length=100)
    missing_business_meaning: list[str] = Field(default_factory=list, max_length=50)


class ValidationIssue(ContractModel):
    """确定性校验问题。"""

    code: str
    path: str
    message: str
    repairable: bool = True


class SemanticDecisionContent(ContractModel):
    """表列语义长期记忆的规范内容。"""

    kind: Literal[MemoryKind.SEMANTIC_DECISION]
    trust: Literal["model_validated", "user_confirmed"] = "model_validated"
    table: SemanticTable | None = None
    column: SemanticColumn | None = None

    @model_validator(mode="after")
    def validate_decision_shape(self) -> SemanticDecisionContent:
        """表或列决策必须且只能包含一种对象。"""
        if (self.table is None) == (self.column is None):
            raise ValueError("语义决策必须且只能包含 table 或 column")
        return self


class MetricQuestionContent(ContractModel):
    """指标问题长期记忆的规范内容。"""

    kind: Literal[MemoryKind.METRIC_QUESTION]
    trust: Literal["model_validated"] = "model_validated"
    question: MetricQuestion


class UserAnswerContent(ContractModel):
    """用户回答长期记忆的规范内容。"""

    kind: Literal[MemoryKind.USER_ANSWER]
    trust: Literal["user_confirmed"] = "user_confirmed"
    answer: MetricAnswer


class MetricDefinitionContent(ContractModel):
    """指标定义长期记忆的规范内容。"""

    kind: Literal[MemoryKind.METRIC_DEFINITION]
    trust: Literal["model_validated", "user_confirmed"] = "model_validated"
    metric: MetricMetadata


MemoryContent = Annotated[
    SemanticDecisionContent
    | MetricQuestionContent
    | UserAnswerContent
    | MetricDefinitionContent,
    Field(discriminator="kind"),
]
MEMORY_CONTENT_ADAPTER = TypeAdapter(MemoryContent)


class MemoryPayload(ContractModel):
    """可从规范内容重建的检索载荷。"""

    version: str
    trust: Literal["model_validated", "user_confirmed"]
    object_ids: list[str]
    tags: list[str]
    model: str | None = None
    prompt_version: str
    graph_version: str


class MemoryCandidate(ContractModel):
    """与 Meta 快照一并提交的长期记忆候选。"""

    uid: str
    source: str
    kind: MemoryKind
    scope_key: str
    schema_fingerprint: str
    pinned: bool = False
    content: MemoryContent
    payload: MemoryPayload
    content_version: str
    reference_uids: list[str] = Field(default_factory=list)
    comment_uids: list[str] = Field(default_factory=list)
    supersedes_uids: list[str] = Field(default_factory=list)


class MemoryRelation(ContractModel):
    """浏览器可见的记忆关系。"""

    relation_type: MemoryRelationType
    memory_uid: str
    related_memory_uid: str


class MemoryListItem(ContractModel):
    """有界记忆列表项。"""

    uid: str
    source: str
    kind: MemoryKind
    scope_key: str
    schema_fingerprint: str
    row_status: MemoryRowStatus
    pinned: bool
    summary: str
    created_at: datetime
    updated_at: datetime


class MemoryDetail(MemoryListItem):
    """有界记忆详情。"""

    content: MemoryContent
    payload: MemoryPayload
    relations: list[MemoryRelation]


class MemoryPage(ContractModel):
    """游标分页的记忆列表。"""

    items: list[MemoryListItem]
    next_cursor: str | None = None


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


class DdlJobRequest(ContractModel):
    """DDL 任务提交请求。"""

    source: str = Field(min_length=1, max_length=128, pattern=r"^[\w.-]+$")
    dialect: Literal["mysql"] = "mysql"
    ddl: str = Field(min_length=1)


class DdlJobAccepted(ContractModel):
    """DDL 任务受理响应。"""

    job_id: str
    status: Literal[JobStatus.PENDING] = JobStatus.PENDING
    status_url: str


class AnswerRequest(ContractModel):
    """问题回答提交请求。"""

    revision: int = Field(ge=0)
    question_set_id: str
    answers: list[MetricAnswer] = Field(min_length=1, max_length=100)


class MemoryPatchRequest(ContractModel):
    """记忆 pin/archive 管理请求。"""

    pinned: bool | None = None
    row_status: Literal[MemoryRowStatus.ARCHIVED] | None = None

    @model_validator(mode="after")
    def validate_single_change(self) -> MemoryPatchRequest:
        """一次请求只允许一种管理变更。"""
        if (self.pinned is None) == (self.row_status is None):
            raise ValueError("必须且只能提供 pinned 或 row_status")
        return self


class MemoryCorrectionRequest(ContractModel):
    """结构化记忆修正请求。"""

    content: MemoryContent


class MemoryCorrectionResponse(ContractModel):
    """记忆修正响应。"""

    memory_uid: str
    supersedes_uid: str
    requires_reprocess: Literal[True] = True


class PayloadRebuildResult(ContractModel):
    """记忆载荷批量重建结果。"""

    processed: int
    succeeded: int
    failed: int
    next_after_id: int | None = None
