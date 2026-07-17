"""Meta 与长期记忆表的 SQLAlchemy Core 定义。"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    Text,
    func,
)

from data_agent.settings import app_config

metadata = MetaData()

table_info = Table(
    "table_info",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(128)),
    Column("role", String(32)),
    Column("description", Text),
)

column_info = Table(
    "column_info",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(128)),
    Column("type", String(64)),
    Column("role", String(32)),
    Column("examples", JSON),
    Column("description", Text),
    Column("alias", JSON),
    Column("table_id", String(64)),
)

metric_info = Table(
    "metric_info",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(128)),
    Column("description", Text),
    Column("relevant_columns", JSON),
    Column("alias", JSON),
)

column_metric = Table(
    "column_metric",
    metadata,
    Column("column_id", String(64), primary_key=True),
    Column("metric_id", String(64), primary_key=True),
)

llm_memory = Table(
    "llm_memory",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("uid", String(64), nullable=False, unique=True),
    Column("source", String(128), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("scope_key", String(256), nullable=False),
    Column("schema_fingerprint", String(64), nullable=False),
    Column("row_status", String(16), nullable=False),
    Column("pinned", Boolean, nullable=False),
    Column("content", JSON, nullable=False),
    Column("payload", JSON, nullable=False),
    Column("content_version", String(32), nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column(
        "updated_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    ),
    schema=app_config.memory.database,
)

llm_memory_relation = Table(
    "llm_memory_relation",
    metadata,
    Column("memory_id", BigInteger, primary_key=True),
    Column("related_memory_id", BigInteger, primary_key=True),
    Column("relation_type", String(32), primary_key=True),
    schema=app_config.memory.database,
)
