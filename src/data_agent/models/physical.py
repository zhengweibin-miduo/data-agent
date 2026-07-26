"""由 DDL AST 确定的物理模式契约。"""

from typing import Literal

from data_agent.models.base import ContractModel


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
