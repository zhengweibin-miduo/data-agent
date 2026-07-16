"""Meta 表信息 ORM 实体。"""

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.model.base import Base


class TableInfoMySQL(Base):
    """映射 ``meta.table_info``。"""

    __tablename__ = "table_info"
    __table_args__ = {"schema": "meta"}

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128))
    role: Mapped[str | None] = mapped_column(String(32))
    description: Mapped[str | None] = mapped_column(Text)
