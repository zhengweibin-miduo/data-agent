"""Mem0 风格长期语义记忆的领域契约。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, TypeAdapter, model_validator

from models.base import ContractModel
from models.semantic import (
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

    trust: Literal["model_validated", "user_confirmed"] = Field(
        default="model_validated", description="内容可信来源。"
    )
    table: SemanticTable | None = Field(default=None, description="表语义决策内容。")
    column: SemanticColumn | None = Field(default=None, description="列语义决策内容。")

    @model_validator(mode="after")
    def validate_decision_shape(self) -> SemanticDecisionContent:
        """表或列决策必须且只能包含一种对象。"""
        if (self.table is None) == (self.column is None):
            raise ValueError("语义决策必须且只能包含 table 或 column")
        return self


class MetricDefinitionContent(ContractModel):
    """指标定义长期记忆的规范内容。"""

    trust: Literal["model_validated", "user_confirmed"] = Field(
        default="model_validated", description="内容可信来源。"
    )
    metric: MetricMetadata = Field(description="指标定义。")
    questions: list[MetricQuestion] = Field(
        default_factory=list, description="问题列表。"
    )
    answers: list[MetricAnswer] = Field(default_factory=list, description="回答列表。")


class UserMemoryContent(ContractModel):
    """由用户原文证据支持的跨会话长期记忆。"""

    trust: Literal["user_confirmed"] = Field(
        default="user_confirmed", description="内容可信来源。"
    )
    value: str = Field(min_length=1, max_length=4096, description="记忆值。")
    supporting_user_quote: str = Field(
        min_length=1, max_length=4096, description="支持内容的用户原文。"
    )
    evidence_message_uids: list[str] = Field(
        min_length=1, max_length=20, description="证据消息标识列表。"
    )
    confirmed_assistant_message_uid: str | None = Field(
        default=None, description="确认内容的助手消息标识。"
    )


class GenericMemoryContent(ContractModel):
    """由类别策略验证的扩展记忆内容。"""

    trust: Literal["model_validated", "user_confirmed"] = Field(
        description="内容可信来源。"
    )
    data: dict[str, Any] = Field(description="扩展数据。")


MemoryContent = (
    SemanticDecisionContent
    | MetricDefinitionContent
    | UserMemoryContent
    | GenericMemoryContent
)
MEMORY_CONTENT_ADAPTER = TypeAdapter(MemoryContent)


class MemoryProjection(ContractModel):
    """ES 与 Qdrant 共享的有界索引投影。"""

    memory_uid: str = Field(description="记忆唯一标识。")
    source: str = Field(description="数据来源标识。")
    user_id: str | None = Field(default=None, description="用户标识。")
    created_conversation_uid: str | None = Field(
        default=None, description="创建会话标识。"
    )
    created_message_uid: str | None = Field(default=None, description="创建消息标识。")
    category: str = Field(
        pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$", description="对象类别。"
    )
    memory_key: str = Field(min_length=1, max_length=256, description="记忆键。")
    content_schema: str = Field(
        min_length=1, max_length=128, description="内容结构类型。"
    )
    schema_fingerprint: str | None = Field(default=None, description="结构指纹。")
    memory_text: str = Field(description="用于检索的记忆文本。")
    content_hash: str = Field(description="内容哈希。")
    object_ids: list[str] = Field(description="关联对象标识列表。")
    trust: MemoryTrust = Field(description="内容可信来源。")
    status: MemoryStatus = Field(description="当前状态。")
    importance_score: float = Field(ge=0, le=1, description="重要性评分。")
    lifecycle_policy: MemoryLifecyclePolicy = Field(description="生命周期策略。")
    expires_at: datetime | None = Field(default=None, description="过期时间。")
    record_version: int = Field(ge=1, description="记录版本。")
    content_version: str = Field(description="内容版本。")
    projection_version: str = Field(description="投影版本。")
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="最近更新时间。")


class MemoryCandidate(ContractModel):
    """与 Meta 快照一并提交的权威记忆候选。"""

    uid: str = Field(description="对象唯一标识。")
    source: str = Field(description="数据来源标识。")
    user_id: str | None = Field(default=None, description="用户标识。")
    created_conversation_uid: str | None = Field(
        default=None, description="创建会话标识。"
    )
    created_message_uid: str | None = Field(default=None, description="创建消息标识。")
    category: str = Field(
        pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$", description="对象类别。"
    )
    memory_key: str = Field(min_length=1, max_length=256, description="记忆键。")
    content_schema: str = Field(
        min_length=1, max_length=128, description="内容结构类型。"
    )
    schema_fingerprint: str | None = Field(default=None, description="结构指纹。")
    memory_text: str = Field(description="用于检索的记忆文本。")
    content: MemoryContent = Field(description="对象内容。")
    content_hash: str = Field(description="内容哈希。")
    trust: MemoryTrust = Field(description="内容可信来源。")
    content_version: str = Field(description="内容版本。")
    projection_version: str = Field(description="投影版本。")
    importance_score: float = Field(default=0.5, ge=0, le=1, description="重要性评分。")
    lifecycle_policy: MemoryLifecyclePolicy = Field(
        default=MemoryLifecyclePolicy.ADAPTIVE, description="生命周期策略。"
    )
    expires_at: datetime | None = Field(default=None, description="过期时间。")
    decision: MemoryDecision | None = Field(default=None, description="写入决策。")
    actor_type: MemoryActorType = Field(
        default=MemoryActorType.WORKFLOW, description="执行者类型。"
    )
    created_job_id: str | None = Field(default=None, description="创建任务标识。")
    derived_from_uids: list[str] = Field(
        default_factory=list, description="派生来源记忆标识列表。"
    )
    related_uids: list[str] = Field(
        default_factory=list, description="关联记忆标识列表。"
    )
    supersedes_uids: list[str] = Field(
        default_factory=list, description="被替代记忆标识列表。"
    )


class MemoryLink(ContractModel):
    """浏览器可见的记忆关联。"""

    link_type: MemoryLinkType = Field(description="记忆关联类型。")
    memory_uid: str = Field(description="记忆唯一标识。")
    linked_memory_uid: str = Field(description="目标记忆标识。")


class MemoryDetail(ContractModel):
    """来自 MySQL 权威内容的有界记忆详情。"""

    uid: str = Field(description="对象唯一标识。")
    source: str = Field(description="数据来源标识。")
    user_id: str | None = Field(default=None, description="用户标识。")
    created_conversation_uid: str | None = Field(
        default=None, description="创建会话标识。"
    )
    created_message_uid: str | None = Field(default=None, description="创建消息标识。")
    category: str = Field(
        pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$", description="对象类别。"
    )
    memory_key: str = Field(min_length=1, max_length=256, description="记忆键。")
    content_schema: str = Field(
        min_length=1, max_length=128, description="内容结构类型。"
    )
    schema_fingerprint: str | None = Field(default=None, description="结构指纹。")
    memory_text: str = Field(description="用于检索的记忆文本。")
    content: MemoryContent = Field(description="对象内容。")
    content_hash: str = Field(description="内容哈希。")
    trust: MemoryTrust = Field(description="内容可信来源。")
    status: MemoryStatus = Field(description="当前状态。")
    importance_score: float = Field(ge=0, le=1, description="重要性评分。")
    lifecycle_policy: MemoryLifecyclePolicy = Field(description="生命周期策略。")
    expires_at: datetime | None = Field(default=None, description="过期时间。")
    record_version: int = Field(ge=1, description="记录版本。")
    access_count: int = Field(ge=0, description="访问次数。")
    last_accessed_at: datetime | None = Field(
        default=None, description="最近访问时间。"
    )
    content_version: str = Field(description="内容版本。")
    projection_version: str = Field(description="投影版本。")
    created_job_id: str | None = Field(default=None, description="创建任务标识。")
    created_at: datetime = Field(description="创建时间。")
    updated_at: datetime = Field(description="最近更新时间。")
    deleted_at: datetime | None = Field(default=None, description="删除时间。")
    purge_requested_at: datetime | None = Field(
        default=None, description="清理请求时间。"
    )
    links: list[MemoryLink] = Field(default_factory=list, description="关联列表。")


class MemorySearchHit(ContractModel):
    """经过 MySQL 回查的混合检索结果。"""

    memory: MemoryDetail = Field(description="命中的记忆详情。")
    score: float = Field(ge=0, description="检索相关性得分。")
    signals: list[str] = Field(description="检索信号列表。")


class MemorySearchResponse(ContractModel):
    """有界混合检索响应。"""

    items: list[MemorySearchHit] = Field(description="列表项。")
    degraded_targets: list[MemoryIndexTarget] = Field(
        default_factory=list, description="降级或不可用的索引目标。"
    )


class MemoryEvent(ContractModel):
    """一条有界的只追加历史事件。"""

    id: int = Field(description="记录唯一标识。")
    memory_uid: str = Field(description="记忆唯一标识。")
    event_type: MemoryEventType = Field(description="事件类型。")
    old_content: MemoryContent | None = Field(default=None, description="变更前内容。")
    new_content: MemoryContent | None = Field(default=None, description="变更后内容。")
    job_id: str | None = Field(default=None, description="任务唯一标识。")
    actor_type: MemoryActorType = Field(description="执行者类型。")
    created_at: datetime = Field(description="创建时间。")


class MemoryHistoryPage(ContractModel):
    """偏移分页的记忆历史。"""

    items: list[MemoryEvent] = Field(description="列表项。")
    offset: int = Field(description="分页偏移。")
    limit: int = Field(description="分页大小。")
    has_more: bool = Field(description="是否还有更多数据。")


class MemoryUpdateRequest(ContractModel):
    """同种类、同作用域的结构化用户修正。"""

    content: MemoryContent = Field(description="对象内容。")
    expected_version: int = Field(ge=1, description="期望的当前记录版本。")


class MemoryUpdateResponse(ContractModel):
    """用户确认修正响应；requires_reprocess 指示是否需 DDL 重处理。"""

    memory_uid: str = Field(description="记忆唯一标识。")
    event_id: int = Field(description="事件唯一标识。")
    record_version: int = Field(ge=1, description="记录版本。")
    requires_reprocess: bool = Field(description="是否需要重新处理。")


class MemoryDeleteResponse(ContractModel):
    """可审计软删除响应。"""

    memory_uid: str = Field(description="记忆唯一标识。")
    deleted: Literal[True] = Field(default=True, description="是否已删除。")


class MemoryOutboxItem(ContractModel):
    """worker 领取的一条索引期望状态。"""

    memory_uid: str = Field(description="记忆唯一标识。")
    target: MemoryIndexTarget = Field(description="索引目标。")
    operation: MemoryIndexOperation = Field(description="索引操作。")
    projection_version: str = Field(description="投影版本。")
    attempts: int = Field(description="处理尝试次数。")
    lease_token: str = Field(description="本次领取的不可复用代次令牌。")


class MemoryRebuildResult(ContractModel):
    """全量重建批次结果。"""

    processed: int = Field(description="已处理数量。")
    next_after_id: int | None = Field(default=None, description="下一页游标标识。")
