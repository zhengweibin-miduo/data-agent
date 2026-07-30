"""Meta 索引 desired state 表。"""

from sqlalchemy import Column, DateTime, Index, Integer, String, Table, func

from data_agent.persistence.schema import metadata
from data_agent.settings import app_config

metadata_index_outbox = Table(
    "metadata_index_outbox",
    metadata,
    Column("target", String(16), primary_key=True),
    Column("object_kind", String(16), primary_key=True),
    Column("object_id", String(128), primary_key=True),
    Column("operation", String(16), nullable=False),
    Column("desired_version", String(64), nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("available_at", DateTime, nullable=False, server_default=func.now()),
    Column("lease_token", String(32), nullable=True),
    Column("lease_expires_at", DateTime, nullable=True),
    Column("last_error_type", String(128), nullable=True),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column(
        "updated_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    ),
    Index(
        "idx_metadata_index_outbox_claim",
        "available_at",
        "lease_expires_at",
        "attempts",
    ),
    schema=app_config.data_sync.database,
)
