"""Mem0 风格长期记忆表的 SQLAlchemy Core 定义。"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    func,
)

from data_agent.persistence.schema import metadata
from data_agent.settings import app_config

agent_memory = Table(
    "agent_memory",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("uid", String(64), nullable=False, unique=True),
    Column("source", String(128), nullable=False),
    Column("user_id", String(128), nullable=True),
    Column("category", String(128), nullable=False),
    Column("memory_key", String(256), nullable=False),
    Column("active_key", String(64), nullable=True, unique=True),
    Column("content_schema", String(128), nullable=False),
    Column("schema_fingerprint", String(64), nullable=True),
    Column("memory_text", Text, nullable=False),
    Column("memory_text_hash", String(64), nullable=False),
    Column("content", JSON, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("trust", String(32), nullable=False),
    Column("status", String(16), nullable=False),
    Column("importance_score", Float, nullable=False),
    Column("lifecycle_policy", String(32), nullable=False),
    Column("expires_at", DateTime, nullable=True),
    Column("record_version", Integer, nullable=False, server_default="1"),
    Column("access_count", BigInteger, nullable=False, server_default="0"),
    Column("last_accessed_at", DateTime, nullable=True),
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
    Index(
        "idx_agent_memory_exact",
        "source",
        "category",
        "memory_key",
        "schema_fingerprint",
        "status",
    ),
    Index("idx_agent_memory_rebuild", "status", "id"),
    Index("idx_agent_memory_user", "user_id", "category", "status", "updated_at"),
    Index("idx_agent_memory_expiry", "status", "expires_at", "id"),
    # 精确基线检索按 source + 定长文本哈希等值查找；memory_text 是 TEXT 列，
    # 直接全等比较无法走索引，会退化为按 source 范围扫描。
    Index(
        "idx_agent_memory_text_hash",
        "source",
        "memory_text_hash",
        "status",
    ),
    # 同一查询的 memory_key 分支也需要独立的等值路径：默认检索允许不带 category，
    # 而 idx_agent_memory_exact 的 category 排在 memory_key 之前，缺少它时无法用
    # 该索引前缀直接定位 memory_key，OR 的这一侧仍会退化为按 source 扫描。
    Index(
        "idx_agent_memory_key_lookup",
        "source",
        "memory_key",
        "status",
    ),
    schema=app_config.memory.database,
)
agent_memory_event = Table(
    "agent_memory_event",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column(
        "memory_id",
        BigInteger,
        ForeignKey(
            f"{app_config.memory.database}.agent_memory.id",
            name="fk_agent_memory_event_memory",
        ),
        nullable=False,
    ),
    Column("event_type", String(16), nullable=False),
    Column("old_content", JSON, nullable=True),
    Column("new_content", JSON, nullable=True),
    Column("job_id", String(64), nullable=True),
    Column("actor_type", String(16), nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Index("idx_agent_memory_event_history", "memory_id", "id"),
    schema=app_config.memory.database,
)
agent_memory_link = Table(
    "agent_memory_link",
    metadata,
    Column(
        "memory_id",
        BigInteger,
        ForeignKey(
            f"{app_config.memory.database}.agent_memory.id",
            name="fk_agent_memory_link_memory",
        ),
        primary_key=True,
    ),
    Column(
        "linked_memory_id",
        BigInteger,
        ForeignKey(
            f"{app_config.memory.database}.agent_memory.id",
            name="fk_agent_memory_link_linked",
        ),
        primary_key=True,
    ),
    Column("link_type", String(32), primary_key=True),
    schema=app_config.memory.database,
)
memory_index_outbox = Table(
    "memory_index_outbox",
    metadata,
    Column(
        "memory_uid",
        String(64),
        ForeignKey(
            f"{app_config.memory.database}.agent_memory.uid",
            name="fk_memory_index_outbox_memory",
        ),
        primary_key=True,
    ),
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
    Index("idx_memory_index_outbox_claim", "available_at", "updated_at"),
    schema=app_config.memory.database,
)
