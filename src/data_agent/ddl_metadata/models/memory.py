"""长期记忆内容、关系与管理契约。"""

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


class MemoryRowStatus(StrEnum):
    """长期记忆行状态。"""

    NORMAL = "NORMAL"
    ARCHIVED = "ARCHIVED"


class MemoryRelationType(StrEnum):
    """长期记忆关系类型。"""

    REFERENCE = "REFERENCE"
    COMMENT = "COMMENT"
    SUPERSEDES = "SUPERSEDES"


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
