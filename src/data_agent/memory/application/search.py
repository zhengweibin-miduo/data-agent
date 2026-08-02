"""基于权威回查的 Long-term Memory 混合检索用例。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

from loguru import logger

from data_agent.memory.application.contracts import (
    EmbeddingProvider,
    LexicalMemoryIndex,
    MemorySearchConfig,
    MemorySearchStore,
    VectorMemoryIndex,
)
from data_agent.memory.domain.payloads import content_object_ids, memory_content_hash
from data_agent.memory.domain.policies import category_policy
from data_agent.memory.domain.ranking import reciprocal_rank_fusion
from data_agent.memory.versions import category_content_version
from data_agent.models.memory import (
    MemoryIndexTarget,
    MemorySearchHit,
    MemorySearchResponse,
    MemoryStatus,
)


def _log_degraded_index(error: BaseException, target: MemoryIndexTarget) -> None:
    """记录派生索引降级，不暴露外部异常内容。"""
    del error
    logger.warning(f"{target.value} 记忆索引检索失败，本次查询已降级为其余可用信号")


class MemorySearchService:
    """并发检索派生信号并仅返回 MySQL 复核后的权威内容。"""

    def __init__(
        self,
        store: MemorySearchStore,
        lexical_index: LexicalMemoryIndex,
        vector_index: VectorMemoryIndex,
        embeddings: EmbeddingProvider,
        config: MemorySearchConfig,
    ) -> None:
        """绑定权威 store、可降级索引端口与显式预算。"""
        self._store = store
        self._lexical_index = lexical_index
        self._vector_index = vector_index
        self._embeddings = embeddings
        self._config = config

    async def search(
        self,
        query: str,
        source: str,
        *,
        user_id: str | None = None,
        categories: set[str] | None = None,
        limit: int | None = None,
        exact_uids: Sequence[str] = (),
        allowed_object_ids: set[str] | None = None,
    ) -> MemorySearchResponse:
        """执行稳定 RRF，并在各远程路径失败时独立降级。"""
        bounded_limit = min(
            limit or self._config.search_limit,
            self._config.search_limit,
        )
        # 步骤一：保留调用方精确候选；否则读取 MySQL 精确基线。
        baseline_uids = (
            list(exact_uids)
            if exact_uids
            else await self._store.find_exact(
                source,
                query,
                categories,
                user_id=user_id,
                limit=bounded_limit,
            )
        )

        # 步骤二：词法和向量路径并发且分别限时，一个失败不取消另一个信号。
        async def lexical_search() -> list[str]:
            return await self._lexical_index.search(
                query,
                source,
                categories,
                self._config.lexical_top_k,
                user_id=user_id,
            )

        async def vector_search() -> list[str]:
            vector = await self._embeddings.embed_query(query)
            return await self._vector_index.search(
                vector,
                source,
                categories,
                self._config.vector_top_k,
                user_id=user_id,
            )

        results = await asyncio.gather(
            asyncio.wait_for(lexical_search(), timeout=self._config.timeout_seconds),
            asyncio.wait_for(vector_search(), timeout=self._config.timeout_seconds),
            return_exceptions=True,
        )
        rankings: list[tuple[str, Sequence[str]]] = []
        degraded: list[MemoryIndexTarget] = []
        for target, signal, result in (
            (MemoryIndexTarget.ELASTICSEARCH, "elasticsearch", results[0]),
            (MemoryIndexTarget.QDRANT, "qdrant", results[1]),
        ):
            if isinstance(result, BaseException):
                degraded.append(target)
                _log_degraded_index(result, target)
            else:
                rankings.append((signal, result))

        # 步骤三：索引只贡献 UID；统一回查同租户权威内容及未收敛目标。
        candidate_uids = {
            *baseline_uids,
            *(uid for _signal, uids in rankings for uid in uids),
        }
        if not candidate_uids:
            return MemorySearchResponse(items=[], degraded_targets=degraded)
        memories = await self._store.load_authority(candidate_uids, user_id=user_id)
        pending_targets = await self._store.pending_targets(candidate_uids)
        by_uid = {memory.uid: memory for memory in memories}

        # 步骤四：只剔除该目标仍待收敛的排名信号。
        target_by_signal = {
            "elasticsearch": MemoryIndexTarget.ELASTICSEARCH,
            "qdrant": MemoryIndexTarget.QDRANT,
        }
        confirmed_rankings = [
            (
                signal,
                [
                    uid
                    for uid in uids
                    if target_by_signal[signal] not in pending_targets.get(uid, set())
                ],
            )
            for signal, uids in rankings
        ]
        fused = reciprocal_rank_fusion(
            [("mysql_exact", baseline_uids), *confirmed_rankings],
            constant=self._config.rrf_constant,
            exact_uids=set(baseline_uids),
        )

        # 步骤五：按租户、来源、类别、状态、内容版本/哈希、有效期与对象范围复核。
        items: list[MemorySearchHit] = []
        now = datetime.now(UTC).replace(tzinfo=None)
        for uid, score, signals in fused:
            detail = by_uid.get(uid)
            if detail is None:
                continue
            if (
                detail.source != source
                or detail.user_id != user_id
                or detail.status != MemoryStatus.ACTIVE
                or detail.content_version != category_content_version(detail.category)
                or memory_content_hash(detail.content) != detail.content_hash
                or (categories and detail.category not in categories)
                or (detail.expires_at is not None and detail.expires_at <= now)
            ):
                continue
            object_ids = set(content_object_ids(detail.content))
            if (
                allowed_object_ids is not None
                and object_ids
                and not object_ids.issubset(allowed_object_ids)
            ):
                continue
            items.append(
                MemorySearchHit(
                    memory=detail,
                    score=(
                        score * category_policy(detail.category).retrieval_weight
                        + detail.importance_score * 0.01
                    ),
                    signals=signals,
                )
            )
        items.sort(key=lambda item: (-item.score, item.memory.uid))
        items = items[:bounded_limit]

        # 步骤六：访问统计是尽力写入，失败不得撤销已完成的权威过滤结果。
        if items:
            try:
                await self._store.record_access(
                    {item.memory.uid for item in items},
                    source=source,
                    user_id=user_id,
                )
            except Exception:  # noqa: BLE001
                logger.warning("记忆访问统计写入失败，搜索结果已按尽力而为返回")
        return MemorySearchResponse(items=items, degraded_targets=degraded)
