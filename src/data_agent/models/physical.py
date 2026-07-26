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


class PhysicalSchema(ContractModel):
    """规范化后的完整物理模式。"""

    source: str = Field(description="数据来源标识。")
    canonical_ddl: str = Field(description="规范化 DDL 文本。")
    ddl_hash: str = Field(description="DDL 内容哈希。")
    schema_fingerprint: str = Field(description="结构指纹。")
    tables: list[PhysicalTable] = Field(description="表列表。")
