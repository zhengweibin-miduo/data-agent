"""Conversation Memory 提炼的 MySQL claim 与 commit 适配器。"""

from conversation.models import ClaimedExtraction
from conversation.repository import ConversationRepository
from infrastructure.mysql import MySQLDatabase
from memory.mysql.repository import MemoryRepository
from models.memory import MemoryCandidate


class MySQLExtractionClaimStore:
    """用短事务实现提炼 claim 与 retry。"""

    def __init__(self, *, max_backoff_seconds: int) -> None:
        """绑定提炼失败的最大退避秒数。"""
        self._max_backoff_seconds = max_backoff_seconds

    async def claim(
        self, *, limit: int, lease_seconds: int, message_limit: int
    ) -> list[ClaimedExtraction]:
        """领取并提交一个有界任务波次。"""
        async with MySQLDatabase.session() as session:
            return await ConversationRepository(session).claim_extractions(
                limit=limit,
                lease_seconds=lease_seconds,
                message_limit=message_limit,
            )

    async def retry(self, claim: ClaimedExtraction, error_type: str) -> None:
        """释放 lease 并登记数据库时钟退避。"""
        async with MySQLDatabase.session() as session:
            await ConversationRepository(session).retry_extraction(
                claim,
                error_type,
                self._max_backoff_seconds,
            )


class MySQLExtractionCommitter:
    """原子提交 Memory candidate 与 Conversation 提炼完成态。"""

    async def commit(
        self,
        claim: ClaimedExtraction,
        candidates: list[MemoryCandidate],
        summary: str,
    ) -> None:
        """在一个事务内写入两个 bounded context 的权威状态。"""
        async with MySQLDatabase.session() as session:
            # 步骤一：先沿既有 Memory authority path upsert 已验证 proposals。
            await MemoryRepository(session).upsert_candidates(candidates)
            # 步骤二：同一事务按 lease CAS 推进摘要并删除 extraction outbox。
            finished = await ConversationRepository(session).finish_extraction(
                claim, summary
            )
            if not finished:
                raise RuntimeError("提炼 lease 已失效")
