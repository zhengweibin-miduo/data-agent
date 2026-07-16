"""字段值业务实体。"""

from dataclasses import dataclass


@dataclass(slots=True)
class ValueInfo:
    """写入 Elasticsearch 的一个字段值。"""

    id: str
    value: str
    column_id: str
