"""Meta 字段指标关系 ORM 实体。"""

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.model.base import Base


class ColumnMetricMySQL(Base):
    """映射 ``meta.column_metric``。"""

    __tablename__ = "column_metric"
    __table_args__ = {"schema": "meta"}

    column_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    metric_id: Mapped[str] = mapped_column(String(64), primary_key=True)
