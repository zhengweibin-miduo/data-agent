"""锁定长期记忆 Core 元数据与 bootstrap DDL 的关键约束。"""

from data_agent.memory.mysql.tables import (
    agent_memory,
    agent_memory_event,
    agent_memory_link,
    memory_index_outbox,
)
from data_agent.settings import app_config
from tests.helpers.checks import check_equal


def test_memory_table_indexes_match_bootstrap_contract() -> None:
    """Core create_all 必须生成 bootstrap 声明的查询索引。"""
    check_equal(
        "agent_memory 索引",
        {index.name for index in agent_memory.indexes},
        {
            "idx_agent_memory_exact",
            "idx_agent_memory_rebuild",
            "idx_agent_memory_user",
            "idx_agent_memory_expiry",
        },
    )
    check_equal(
        "agent_memory_event 索引",
        {index.name for index in agent_memory_event.indexes},
        {"idx_agent_memory_event_history"},
    )
    check_equal(
        "memory_index_outbox 索引",
        {index.name for index in memory_index_outbox.indexes},
        {"idx_memory_index_outbox_claim"},
    )


def test_memory_table_foreign_keys_match_bootstrap_contract() -> None:
    """历史、关联和投影 outbox 不得指向不存在的权威记忆。"""
    database = app_config.memory.database
    foreign_keys = {
        (
            foreign_key.parent.table.name,
            foreign_key.parent.name,
            foreign_key.target_fullname,
            (
                foreign_key.constraint.name
                if foreign_key.constraint is not None
                else None
            ),
        )
        for table in (agent_memory_event, agent_memory_link, memory_index_outbox)
        for foreign_key in table.foreign_keys
    }
    check_equal(
        "长期记忆外键",
        foreign_keys,
        {
            (
                "agent_memory_event",
                "memory_id",
                f"{database}.agent_memory.id",
                "fk_agent_memory_event_memory",
            ),
            (
                "agent_memory_link",
                "memory_id",
                f"{database}.agent_memory.id",
                "fk_agent_memory_link_memory",
            ),
            (
                "agent_memory_link",
                "linked_memory_id",
                f"{database}.agent_memory.id",
                "fk_agent_memory_link_linked",
            ),
            (
                "memory_index_outbox",
                "memory_uid",
                f"{database}.agent_memory.uid",
                "fk_memory_index_outbox_memory",
            ),
        },
    )
