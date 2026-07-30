"""由 DDL AST 确定的物理模式契约。"""

from typing import Literal

from pydantic import Field

from data_agent.models.base import ContractModel


class PhysicalColumn(ContractModel):
    """由 DDL AST 唯一确定的列信息。"""

    id: str = Field(description="记录唯一标识。")
    name: str = Field(description="对象名称。")
    data_type: str = Field(description="数据类型。")
    comment: str | None = Field(default=None, description="对象注释。")
    nullable: bool = Field(default=True, description="字段是否允许保存空值。")
    structural_role: Literal["primary_key", "foreign_key"] | None = Field(
        default=None, description="结构角色。"
    )


class PhysicalTable(ContractModel):
    """由 DDL AST 唯一确定的表信息。"""

    id: str = Field(description="记录唯一标识。")
    schema_name: str | None = Field(default=None, description="所属 schema 名称。")
    name: str = Field(description="对象名称。")
    qualified_name: str = Field(description="限定名称。")
    comment: str | None = Field(default=None, description="对象注释。")
    columns: list[PhysicalColumn] = Field(description="列列表。")
    primary_key: list[str] = Field(
        default_factory=list,
        description="按 DDL 声明顺序排列的主键字段名称。",
    )


class PhysicalRelationship(ContractModel):
    """由 DDL AST 唯一确定的外键引用边。"""

    source_table_id: str = Field(description="引用方表的唯一标识。")
    source_column_id: str = Field(description="引用方字段的唯一标识。")
    target_table: str = Field(description="被引用表的限定名称。")
    target_column: str = Field(description="被引用字段名称。")


class PhysicalSchema(ContractModel):
    """规范化后的完整物理模式。"""

    source: str = Field(description="数据来源标识。")
    canonical_ddl: str = Field(description="规范化 DDL 文本。")
    ddl_hash: str = Field(description="DDL 内容哈希。")
    schema_fingerprint: str = Field(description="结构指纹。")
    tables: list[PhysicalTable] = Field(description="表列表。")
    relationships: list[PhysicalRelationship] = Field(
        default_factory=list,
        description="由外键约束规范化得到的字段引用边。",
    )
