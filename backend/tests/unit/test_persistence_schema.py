"""跨业务 SQLAlchemy Core 元数据所有权检查。"""

from conversation.mysql_tables import agent_conversation
from ddl_metadata.persistence.tables import table_info
from memory.mysql.tables import agent_memory
from persistence.schema import metadata
from tests.helpers.checks import check_condition


def test_all_feature_tables_share_root_metadata() -> None:
    """验证会话、Meta 与长期记忆表共享同一根级 MetaData。"""
    for table in (agent_conversation, table_info, agent_memory):
        check_condition(
            f"{table.fullname} 使用根级 MetaData",
            table.metadata is metadata,
            expected="同一个 persistence.schema.metadata 实例",
        )
