"""Meta 指标信息 ORM 实体。"""

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.model.base import Base


class MetricInfoMySQL(Base):
    """映射 ``meta.metric_info``。"""

    __tablename__ = "metric_info"
    __table_args__ = {"schema": "meta"}

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(Text)
    relevant_columns: Mapped[list[str] | None] = mapped_column(JSON)
    alias: Mapped[list[str] | None] = mapped_column(JSON)
