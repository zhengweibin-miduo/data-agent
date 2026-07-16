"""字段元数据业务实体。"""

from dataclasses import dataclass


@dataclass(slots=True)
class ColumnInfo:
    """一个 DW 字段的业务元数据。"""

    id: str
    name: str
    type: str
    role: str
    examples: list[str]
    description: str
    alias: list[str]
    table_id: str
