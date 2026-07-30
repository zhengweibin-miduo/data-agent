"""Meta 派生索引的类型化契约。"""

from enum import StrEnum

from pydantic import Field

from data_agent.models.base import ContractModel


class MetadataObjectKind(StrEnum):
    """可检索 Meta 对象类型。"""

    TABLE = "table"
    COLUMN = "column"
    METRIC = "metric"


class MetadataIndexTarget(StrEnum):
    """派生索引目标。"""

    SEMANTIC = "semantic"
    VALUES = "values"


class MetadataIndexOperation(StrEnum):
    """desired state 操作。"""

    UPSERT = "upsert"
    DELETE = "delete"
    REFRESH = "refresh"
    REBUILD = "rebuild"


class MetadataIndexDesired(ContractModel):
    """一条可合并的 Meta 索引期望状态。"""

    target: MetadataIndexTarget = Field(description="索引目标。")
    object_kind: MetadataObjectKind = Field(description="对象类型。")
    object_id: str = Field(min_length=1, max_length=128, description="对象标识。")
    operation: MetadataIndexOperation = Field(description="期望操作。")
    desired_version: str = Field(min_length=1, max_length=64, description="期望版本。")


class ClaimedMetadataIndexWork(MetadataIndexDesired):
    """带租约令牌的已领取索引任务。"""

    lease_token: str = Field(min_length=32, max_length=32, description="领取令牌。")
    progress_column_id: str | None = Field(
        default=None,
        max_length=128,
        description="字段值刷新最后完成的字段标识。",
    )


class MetadataSemanticProjection(ContractModel):
    """Qdrant 中的有界 Meta 语义投影。"""

    kind: MetadataObjectKind = Field(description="Meta 对象类型。")
    object_id: str = Field(description="Meta 对象标识。")
    table_id: str | None = Field(default=None, description="所属表标识。")
    role: str | None = Field(default=None, description="对象角色。")
    data_type: str | None = Field(default=None, description="字段数据类型。")
    search_text: str = Field(min_length=1, description="规范化检索文本。")
    schema_fingerprint: str = Field(description="源数据版本标识。")
    projection_version: str = Field(description="投影结构版本。")


class MetadataValueProjection(ContractModel):
    """Elasticsearch 中的字段值投影。"""

    column_id: str = Field(description="字段标识。")
    table_id: str = Field(description="所属表标识。")
    value_text: str = Field(description="可检索值文本。")
    value_keyword: str = Field(description="精确匹配值。")
    frequency: int = Field(ge=1, description="当前 DW 快照中的出现次数。")
    refresh_version: str = Field(description="表级刷新版本。")
    schema_fingerprint: str = Field(description="源数据版本标识。")


class MetadataCandidate(ContractModel):
    """经 Meta 回读确认的语义候选。"""

    kind: MetadataObjectKind = Field(description="Meta 对象类型。")
    object_id: str = Field(description="Meta 对象标识。")
    table_id: str | None = Field(default=None, description="所属表标识。")
    name: str = Field(description="权威对象名称。")
    description: str = Field(description="权威对象描述。")
    related_column_ids: list[str] = Field(
        default_factory=list,
        description="指标关联的权威字段标识；非指标候选为空。",
    )
    score: float = Field(ge=0, description="融合检索相关度分数。")
    matched_text: str = Field(description="产生召回的有界语义投影文本。")


class MetadataSemanticHit(ContractModel):
    """Qdrant 返回的非权威语义召回信息。"""

    kind: MetadataObjectKind
    object_id: str
    schema_fingerprint: str
    score: float = Field(ge=0)
    matched_text: str


class MetadataValueCandidate(ContractModel):
    """经字段资格确认的值候选。"""

    column_id: str = Field(description="字段标识。")
    table_id: str = Field(description="所属表标识。")
    value: str = Field(description="匹配值。")
    frequency: int = Field(ge=1, description="值频次。")


class MetadataValueSearchResult(ContractModel):
    """字段值候选及完整性。"""

    values: list[MetadataValueCandidate] = Field(description="值候选。")
    complete: bool = Field(description="候选字段所属表是否均完整可用。")


class MetadataRebuildResult(ContractModel):
    """Meta 派生索引重建投递结果。"""

    semantic_objects: int = Field(ge=0, description="已投递的语义对象数。")
    value_tables: int = Field(ge=0, description="已投递的字段值刷新表数。")
