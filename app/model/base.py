"""Meta MySQL ORM 基类。"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Meta MySQL 表实体基类。"""
