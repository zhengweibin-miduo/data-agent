"""表元数据业务实体。"""

from dataclasses import dataclass


@dataclass(slots=True)
class TableInfo:
    """一张 DW 表的业务元数据。"""

    id: str
    name: str
    role: str
    description: str
