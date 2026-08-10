"""Long-term Memory 到 Query 规则候选的防腐适配器。"""

from errors import DataAgentError
from identifiers import CONVERSATION_MEMORY_SOURCE
from memory.application.search import MemorySearchService
from models.memory import (
    BuiltinMemoryCategory,
    MemoryStatus,
    MemoryTrust,
    QueryBindingRuleContent,
)
from query.application.contracts import (
    QueryBindingRuleCandidate,
    QueryBindingRuleRecall,
)


class MemoryQueryBindingRuleAdapter:
    """复用混合召回，并仅投影已由 Memory 权威回查的规则。"""

    def __init__(self, search: MemorySearchService, *, limit: int) -> None:
        """绑定现有 Memory 搜索和 Query 候选预算。"""
        self._search = search
        self._limit = limit

    async def recall(
        self,
        user_id: str,
        query_text: str,
        *,
        exact_aliases: list[str],
    ) -> QueryBindingRuleRecall:
        """召回并投影指定用户的当前规则快照。"""
        try:
            response = await self._search.search(
                query_text,
                CONVERSATION_MEMORY_SOURCE,
                user_id=user_id,
                categories={BuiltinMemoryCategory.USER_QUERY_BINDING_RULE.value},
                limit=self._limit,
                exact_queries=exact_aliases,
            )
        except Exception as error:
            raise DataAgentError(
                "query_rule_unavailable",
                "query_rule_recall",
                "查询规则召回暂不可用，请重试",
                retryable=True,
                http_status=503,
            ) from error
        candidates: list[QueryBindingRuleCandidate] = []
        for hit in response.items:
            memory = hit.memory
            content = memory.content
            if (
                memory.category != BuiltinMemoryCategory.USER_QUERY_BINDING_RULE.value
                or memory.user_id != user_id
                or memory.status != MemoryStatus.ACTIVE
                or memory.trust != MemoryTrust.USER_CONFIRMED
                or not isinstance(content, QueryBindingRuleContent)
            ):
                continue
            candidates.append(
                QueryBindingRuleCandidate(
                    alias=content.alias,
                    target=content.target,
                    memory_uid=memory.uid,
                    record_version=memory.record_version,
                    content_hash=memory.content_hash,
                    score=hit.score,
                    signals=hit.signals,
                )
            )
        return QueryBindingRuleRecall(
            candidates=candidates,
            degraded_targets=[target.value for target in response.degraded_targets],
        )
