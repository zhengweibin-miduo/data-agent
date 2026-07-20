"""Mem0 风格长期记忆表的 SQLAlchemy Core 定义。"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Integer,
    String,
    Table,
    Text,
    func,
)

from data_agent.ddl_metadata.persistence.schema import metadata
from data_agent.settings import app_config

agent_memory = Table(
    "agent_memory",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("uid", String(64), nullable=False, unique=True),
    Column("source", String(128), nullable=False),
    Column("user_id", String(128), nullable=True),
    Column("kind", String(32), nullable=False),
    Column("scope_key", String(256), nullable=False),
    Column("schema_fingerprint", String(64), nullable=False),
    Column("memory_text", Text, nullable=False),
    Column("content", JSON, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("trust", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("content_version", String(32), nullable=False),
    Column("projection_version", String(32), nullable=False),
    Column("created_job_id", String(64), nullable=True),
    Column("created_conversation_uid", String(64), nullable=True),
    Column("created_message_uid", String(64), nullable=True),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column(
        "updated_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    ),
    Column("deleted_at", DateTime, nullable=True),
    Column("purge_requested_at", DateTime, nullable=True),
    schema=app_config.memory.database,
)
agent_memory_event = Table(
    "agent_memory_event",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("memory_id", BigInteger, nullable=False),
    Column("event_type", String(16), nullable=False),
    Column("old_content", JSON, nullable=True),
    Column("new_content", JSON, nullable=True),
    Column("job_id", String(64), nullable=True),
    Column("actor_type", String(16), nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    schema=app_config.memory.database,
)
agent_memory_link = Table(
    "agent_memory_link",
    metadata,
    Column("memory_id", BigInteger, primary_key=True),
    Column("linked_memory_id", BigInteger, primary_key=True),
    Column("link_type", String(32), primary_key=True),
    schema=app_config.memory.database,
)
memory_index_outbox = Table(
    "memory_index_outbox",
    metadata,
    Column("memory_uid", String(64), primary_key=True),
    Column("target", String(16), primary_key=True),
    Column("operation", String(16), nullable=False),
    Column("projection_version", String(32), nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("available_at", DateTime, nullable=False, server_default=func.now()),
    Column("last_error_type", String(128), nullable=True),
    Column(
        "updated_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    ),
    schema=app_config.memory.database,
)
