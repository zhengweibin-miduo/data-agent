"""Conversation 到 Long-term Memory application 的防腐适配器。"""

from identifiers import CONVERSATION_MEMORY_SOURCE
from memory.application.search import MemorySearchService
from models.memory import BuiltinMemoryCategory, MemoryDetail

_CONVERSATION_MEMORY_CATEGORIES = {
    BuiltinMemoryCategory.USER_PROFILE.value,
    BuiltinMemoryCategory.USER_PREFERENCE.value,
    BuiltinMemoryCategory.USER_CONSTRAINT.value,
    BuiltinMemoryCategory.USER_BUSINESS_RULE.value,
}


class MemorySearchLongTermMemoryReader:
    """把 Memory search 响应投影为 Conversation 需要的权威记忆。"""

    def __init__(self, search: MemorySearchService) -> None:
        """绑定 Long-term Memory application search。"""
        self._search = search

    async def recall(
        self, query: str, user_id: str, *, limit: int
    ) -> list[MemoryDetail]:
        """按固定 source/category 和租户边界召回权威记忆。"""
        response = await self._search.search(
            query,
            CONVERSATION_MEMORY_SOURCE,
            user_id=user_id,
            categories=_CONVERSATION_MEMORY_CATEGORIES,
            limit=limit,
        )
        return [hit.memory for hit in response.items]
