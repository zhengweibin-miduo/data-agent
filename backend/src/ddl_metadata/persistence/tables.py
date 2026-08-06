"""Meta 快照表的 SQLAlchemy Core 定义。"""

from sqlalchemy import (
    JSON,
    Column,
    String,
    Table,
    Text,
)

from persistence.schema import metadata

table_info = Table(
    "table_info",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(128)),
    Column("role", String(32)),
    Column("description", Text),
    Column("alias", JSON),
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
    Column("index_profile", JSON, nullable=False),
    Column("table_id", String(64)),
)
metric_info = Table(
    "metric_info",
    metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(128)),
    Column("description", Text),
    Column("fact_table_id", String(64), nullable=False),
    Column("relevant_columns", JSON),
    Column("alias", JSON),
)
column_metric = Table(
    "column_metric",
    metadata,
    Column("column_id", String(64), primary_key=True),
    Column("metric_id", String(64), primary_key=True),
)

physical_schema_authority = Table(
    "physical_schema_authority",
    metadata,
    Column("source", String(128), primary_key=True),
    Column("schema_fingerprint", String(64), nullable=False),
)
