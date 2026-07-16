"""字段指标关系业务实体。"""

from dataclasses import dataclass


@dataclass(slots=True)
class ColumnMetric:
    """字段与指标的关联关系。"""

    column_id: str
    metric_id: str
