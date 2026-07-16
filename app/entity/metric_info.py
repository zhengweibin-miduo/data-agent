"""指标元数据业务实体。"""

from dataclasses import dataclass


@dataclass(slots=True)
class MetricInfo:
    """一个业务指标的元数据。"""

    id: str
    name: str
    description: str
    relevant_columns: list[str]
    alias: list[str]
