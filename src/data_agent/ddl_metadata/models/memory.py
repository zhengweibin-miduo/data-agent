"""Mem0 风格长期语义记忆的领域契约。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from data_agent.ddl_metadata.models.base import ContractModel
from data_agent.ddl_metadata.models.semantic import (
    MetricAnswer,
    MetricMetadata,
    MetricQuestion,
    SemanticColumn,
    SemanticTable,
)


class MemoryKind(StrEnum):
    """长期记忆类型。"""

    SEMANTIC_DECISION = "SEMANTIC_DECISION"
    METRIC_QUESTION = "METRIC_QUESTION"
    USER_ANSWER = "USER_ANSWER"
    METRIC_DEFINITION = "METRIC_DEFINITION"


class MemoryStatus(StrEnum):
    """权威记忆状态。"""

    ACTIVE = "ACTIVE"
    DELETED = "DELETED"


class MemoryTrust(StrEnum):
    """记忆事实的可信来源。"""

    MODEL_VALIDATED = "model_validated"
    USER_CONFIRMED = "user_confirmed"


class MemoryEventType(StrEnum):
    """只追加历史事件类型。"""

    ADD = "ADD"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    LINK = "LINK"


class MemoryActorType(StrEnum):
    """记忆事件执行者类型。"""

    WORKFLOW = "WORKFLOW"
    USER = "USER"
    SYSTEM = "SYSTEM"


class MemoryLinkType(StrEnum):
    """领域记忆关联类型。"""

    RELATED = "RELATED"
    DERIVED_FROM = "DERIVED_FROM"
    SUPERSEDES = "SUPERSEDES"


class MemoryIndexTarget(StrEnum):
    """可重建索引目标。"""

    ELASTICSEARCH = "ELASTICSEARCH"
    QDRANT = "QDRANT"


class MemoryIndexOperation(StrEnum):
    """索引期望操作。"""

    UPSERT = "UPSERT"
    DELETE = "DELETE"


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


class MemoryProjection(ContractModel):
    """ES 与 Qdrant 共享的有界索引投影。"""

    memory_uid: str
    source: str
    kind: MemoryKind
    scope_key: str
    schema_fingerprint: str
    memory_text: str
    content_hash: str
    object_ids: list[str]
    trust: MemoryTrust
    status: MemoryStatus
    content_version: str
    projection_version: str
    created_at: datetime
    updated_at: datetime


class MemoryCandidate(ContractModel):
    """与 Meta 快照一并提交的权威记忆候选。"""

    uid: str
    source: str
    kind: MemoryKind
    scope_key: str
    schema_fingerprint: str
    memory_text: str
    content: MemoryContent
    content_hash: str
    trust: MemoryTrust
    content_version: str
    projection_version: str
    created_job_id: str
    derived_from_uids: list[str] = Field(default_factory=list)
    related_uids: list[str] = Field(default_factory=list)
    supersedes_uids: list[str] = Field(default_factory=list)


class MemoryLink(ContractModel):
    """浏览器可见的记忆关联。"""

    link_type: MemoryLinkType
    memory_uid: str
    linked_memory_uid: str


class MemoryDetail(ContractModel):
    """来自 MySQL 权威内容的有界记忆详情。"""

    uid: str
    source: str
    kind: MemoryKind
    scope_key: str
    schema_fingerprint: str
    memory_text: str
    content: MemoryContent
    content_hash: str
    trust: MemoryTrust
    status: MemoryStatus
    content_version: str
    projection_version: str
    created_job_id: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    links: list[MemoryLink] = Field(default_factory=list)


class MemorySearchHit(ContractModel):
    """经过 MySQL 回查的混合检索结果。"""

    memory: MemoryDetail
    score: float = Field(ge=0)
    signals: list[str]


class MemorySearchResponse(ContractModel):
    """有界混合检索响应。"""

    items: list[MemorySearchHit]
    degraded_targets: list[MemoryIndexTarget] = Field(default_factory=list)


class MemoryEvent(ContractModel):
    """一条有界的只追加历史事件。"""

    id: int
    memory_uid: str
    event_type: MemoryEventType
    old_content: MemoryContent | None = None
    new_content: MemoryContent | None = None
    job_id: str | None = None
    actor_type: MemoryActorType
    created_at: datetime


class MemoryHistoryPage(ContractModel):
    """偏移分页的记忆历史。"""

    items: list[MemoryEvent]
    offset: int
    limit: int
    has_more: bool


class MemoryUpdateRequest(ContractModel):
    """同种类、同作用域的结构化用户修正。"""

    content: MemoryContent


class MemoryUpdateResponse(ContractModel):
    """待重新处理的用户修正响应。"""

    memory_uid: str
    event_id: int
    requires_reprocess: Literal[True] = True


class MemoryDeleteResponse(ContractModel):
    """可审计软删除响应。"""

    memory_uid: str
    deleted: Literal[True] = True


class MemoryOutboxItem(ContractModel):
    """worker 领取的一条索引期望状态。"""

    memory_uid: str
    target: MemoryIndexTarget
    operation: MemoryIndexOperation
    projection_version: str
    attempts: int


class MemoryRebuildResult(ContractModel):
    """全量重建批次结果。"""

    processed: int
    next_after_id: int | None = None
