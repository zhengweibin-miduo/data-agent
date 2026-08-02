"""Conversation 与 Long-term Memory 用户删除事务适配器。"""

from conversation.repository import ConversationRepository
from infrastructure.mysql import MySQLDatabase
from memory.mysql.repository import MemoryRepository


class MySQLUserDataEraser:
    """在单一 MySQL 事务内删除 Conversation 并 tombstone Memory。"""

    async def erase(self, user_id: str) -> None:
        """执行跨上下文原子用户数据删除。"""
        async with MySQLDatabase.session() as session:
            # 步骤一：先删除 Conversation 权威状态，停止后续提炼输入。
            await ConversationRepository(session).delete_user_conversations(user_id)
            # 步骤二：同一事务 tombstone 用户 Long-term Memory 与删除投影请求。
            await MemoryRepository(session).tombstone_user(user_id)
