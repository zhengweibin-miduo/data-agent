"""Meta 字段信息 ORM 实体。"""

from sqlalchemy import JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.model.base import Base


class ColumnInfoMySQL(Base):
    """映射 ``meta.column_info``。"""

    __tablename__ = "column_info"
    __table_args__ = {"schema": "meta"}

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128))
    type: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str | None] = mapped_column(String(32))
    examples: Mapped[list[str] | None] = mapped_column(JSON)
    description: Mapped[str | None] = mapped_column(Text)
    alias: Mapped[list[str] | None] = mapped_column(JSON)
    table_id: Mapped[str | None] = mapped_column(String(64))
