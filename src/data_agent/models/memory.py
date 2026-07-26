"""Mem0 风格长期语义记忆的领域契约。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, TypeAdapter, model_validator

from data_agent.models.base import ContractModel
from data_agent.models.semantic import (
    MetricAnswer,
    MetricMetadata,
    MetricQuestion,
    SemanticColumn,
    SemanticTable,
)


class BuiltinMemoryCategory(StrEnum):
    """内置记忆类别常量；持久化字段允许扩展的点分字符串。"""

    DDL_SEMANTIC = "ddl.semantic"
    DDL_METRIC = "ddl.metric"
    USER_PROFILE = "user.profile"
    USER_PREFERENCE = "user.preference"
    USER_CONSTRAINT = "user.constraint"
    USER_BUSINESS_RULE = "user.business_rule"


class UserMemoryCategory(StrEnum):
    """跨会话用户记忆类别。"""

    PROFILE = "PROFILE"
    PREFERENCE = "PREFERENCE"
    CONSTRAINT = "CONSTRAINT"
    BUSINESS_RULE = "BUSINESS_RULE"


class MemoryStatus(StrEnum):
    """权威记忆状态。"""

    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    DELETED = "DELETED"


class MemoryLifecyclePolicy(StrEnum):
    """长期记忆生命周期策略。"""

    PERMANENT = "PERMANENT"
    ADAPTIVE = "ADAPTIVE"
    EXPIRING = "EXPIRING"
    FINGERPRINT_BOUND = "FINGERPRINT_BOUND"


class MemoryDecision(StrEnum):
    """候选与当前权威事实比较后的写入决策。"""

    ADD = "ADD"
    UPDATE = "UPDATE"
    MERGE = "MERGE"
    DELETE = "DELETE"
    NOOP = "NOOP"


class MemoryScopeType(StrEnum):
    """记忆权威作用域。"""

    USER = "USER"
    DDL_SCHEMA = "DDL_SCHEMA"


class MemoryTrust(StrEnum):
    """记忆事实的可信来源。"""

    MODEL_VALIDATED = "model_validated"
    USER_CONFIRMED = "user_confirmed"


class MemoryEventType(StrEnum):
    """只追加历史事件类型。"""

    ADD = "ADD"
    UPDATE = "UPDATE"
    MERGE = "MERGE"
    DELETE = "DELETE"
    NOOP = "NOOP"
    EXPIRE = "EXPIRE"
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

    trust: Literal["model_validated", "user_confirmed"] = "model_validated"
    table: SemanticTable | None = None
    column: SemanticColumn | None = None

    @model_validator(mode="after")
    def validate_decision_shape(self) -> SemanticDecisionContent:
        """表或列决策必须且只能包含一种对象。"""
        if (self.table is None) == (self.column is None):
            raise ValueError("语义决策必须且只能包含 table 或 column")
        return self


class MetricDefinitionContent(ContractModel):
    """指标定义长期记忆的规范内容。"""

    trust: Literal["model_validated", "user_confirmed"] = "model_validated"
    metric: MetricMetadata
    questions: list[MetricQuestion] = Field(default_factory=list)
    answers: list[MetricAnswer] = Field(default_factory=list)


class UserMemoryContent(ContractModel):
    """由用户原文证据支持的跨会话长期记忆。"""

    trust: Literal["user_confirmed"] = "user_confirmed"
    value: str = Field(min_length=1, max_length=4096)
    supporting_user_quote: str = Field(min_length=1, max_length=4096)
    evidence_message_uids: list[str] = Field(min_length=1, max_length=20)
    confirmed_assistant_message_uid: str | None = None


class GenericMemoryContent(ContractModel):
    """由类别策略验证的扩展记忆内容。"""

    trust: Literal["model_validated", "user_confirmed"]
    data: dict[str, Any]


MemoryContent = (
    SemanticDecisionContent
    | MetricDefinitionContent
    | UserMemoryContent
    | GenericMemoryContent
)
MEMORY_CONTENT_ADAPTER = TypeAdapter(MemoryContent)


class MemoryProjection(ContractModel):
    """ES 与 Qdrant 共享的有界索引投影。"""

    memory_uid: str
    source: str
    user_id: str | None = None
    created_conversation_uid: str | None = None
    created_message_uid: str | None = None
    category: str = Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
    memory_key: str = Field(min_length=1, max_length=256)
    content_schema: str = Field(min_length=1, max_length=128)
    schema_fingerprint: str | None = None
    memory_text: str
    content_hash: str
    object_ids: list[str]
    trust: MemoryTrust
    status: MemoryStatus
    importance_score: float = Field(ge=0, le=1)
    lifecycle_policy: MemoryLifecyclePolicy
    expires_at: datetime | None = None
    record_version: int = Field(ge=1)
    content_version: str
    projection_version: str
    created_at: datetime
    updated_at: datetime


class MemoryCandidate(ContractModel):
    """与 Meta 快照一并提交的权威记忆候选。"""

    uid: str
    source: str
    user_id: str | None = None
    created_conversation_uid: str | None = None
    created_message_uid: str | None = None
    category: str = Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
    memory_key: str = Field(min_length=1, max_length=256)
    content_schema: str = Field(min_length=1, max_length=128)
    schema_fingerprint: str | None = None
    memory_text: str
    content: MemoryContent
    content_hash: str
    trust: MemoryTrust
    content_version: str
    projection_version: str
    importance_score: float = Field(default=0.5, ge=0, le=1)
    lifecycle_policy: MemoryLifecyclePolicy = MemoryLifecyclePolicy.ADAPTIVE
    expires_at: datetime | None = None
    decision: MemoryDecision | None = None
    actor_type: MemoryActorType = MemoryActorType.WORKFLOW
    created_job_id: str | None = None
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
    user_id: str | None = None
    created_conversation_uid: str | None = None
    created_message_uid: str | None = None
    category: str = Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
    memory_key: str = Field(min_length=1, max_length=256)
    content_schema: str = Field(min_length=1, max_length=128)
    schema_fingerprint: str | None = None
    memory_text: str
    content: MemoryContent
    content_hash: str
    trust: MemoryTrust
    status: MemoryStatus
    importance_score: float = Field(ge=0, le=1)
    lifecycle_policy: MemoryLifecyclePolicy
    expires_at: datetime | None = None
    record_version: int = Field(ge=1)
    access_count: int = Field(ge=0)
    last_accessed_at: datetime | None = None
    content_version: str
    projection_version: str
    created_job_id: str | None = None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    purge_requested_at: datetime | None = None
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
    expected_version: int = Field(ge=1)


class MemoryUpdateResponse(ContractModel):
    """待重新处理的用户修正响应。"""

    memory_uid: str
    event_id: int
    record_version: int = Field(ge=1)
    requires_reprocess: bool


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
