"""Agent 对话、消息与记忆提炼 outbox 表。"""

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT

from data_agent.persistence.schema import metadata
from data_agent.settings import app_config

agent_conversation = Table(
    "agent_conversation",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("uid", String(64), nullable=False, unique=True),
    Column("user_id", String(128), nullable=False),
    Column("summary", Text, nullable=True),
    Column("summary_through_message_id", BigInteger, nullable=True),
    Column("active_turn_uid", String(64), nullable=True),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    Column(
        "updated_at",
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    ),
    Index("idx_agent_conversation_user", "user_id", "updated_at", "id"),
    schema=app_config.memory.database,
)

agent_message = Table(
    "agent_message",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("uid", String(64), nullable=False, unique=True),
    Column("user_id", String(128), nullable=False),
    Column(
        "conversation_id",
        BigInteger,
        ForeignKey(
            f"{app_config.memory.database}.agent_conversation.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column("turn_uid", String(64), nullable=False),
    Column("role", String(16), nullable=False),
    Column("content", MEDIUMTEXT, nullable=False),
    Column("created_at", DateTime, nullable=False, server_default=func.now()),
    UniqueConstraint(
        "conversation_id",
        "turn_uid",
        "role",
        name="uq_agent_message_turn_role",
    ),
    Index(
        "idx_agent_message_history",
        "user_id",
        "conversation_id",
        "id",
    ),
    schema=app_config.memory.database,
)

conversation_memory_outbox = Table(
    "conversation_memory_outbox",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("user_id", String(128), nullable=False),
    Column(
        "conversation_id",
        BigInteger,
        ForeignKey(
            f"{app_config.memory.database}.agent_conversation.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    ),
    Column("turn_uid", String(64), nullable=False),
    Column("user_message_id", BigInteger, nullable=False),
    Column("assistant_message_id", BigInteger, nullable=False),
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
    UniqueConstraint(
        "conversation_id",
        "turn_uid",
        name="uq_conversation_memory_turn",
    ),
    Index(
        "idx_conversation_memory_claim",
        "available_at",
        "lease_expires_at",
        "id",
    ),
    schema=app_config.memory.database,
)
