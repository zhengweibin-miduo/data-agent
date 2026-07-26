"""MySQL 权威回查的 ES/Qdrant 混合记忆检索。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

from loguru import logger

from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.infrastructure.tei_embeddings import TEIEmbeddingClient
from data_agent.memory.domain.payloads import (
    content_object_ids,
    memory_content_hash,
)
from data_agent.memory.domain.policies import category_policy
from data_agent.memory.domain.ranking import reciprocal_rank_fusion
from data_agent.memory.indexing.elasticsearch import (
    MemoryElasticsearchIndex,
)
from data_agent.memory.indexing.qdrant import MemoryQdrantIndex
from data_agent.memory.mysql.index_outbox import (
    MemoryIndexOutboxRepository,
)
from data_agent.memory.mysql.repository import MemoryRepository
from data_agent.models.memory import (
    MemoryIndexTarget,
    MemorySearchHit,
    MemorySearchResponse,
    MemoryStatus,
)
from data_agent.settings import app_config


class MemorySearchService:
    """并发检索派生索引并仅返回复核后的 MySQL 权威内容。"""

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
        """执行稳定 RRF，并在索引失败时安全降级。"""
        bounded_limit = min(
            limit or app_config.memory.search_limit,
            app_config.memory.search_limit,
        )
        # 步骤一：优先采用调用方给出的精确 UID；未提供时再查询 MySQL，
        # 使两个派生索引都失败时仍保留可验证的精确基线。
        rankings: list[tuple[str, Sequence[str]]] = []
        degraded: list[MemoryIndexTarget] = []
        if exact_uids:
            baseline_uids = list(exact_uids)
        else:
            async with MySQLDatabase.session() as session:
                baseline_uids = await MemoryRepository(session).find_exact_query(
                    source,
                    query,
                    categories,
                    user_id=user_id,
                    limit=bounded_limit,
                )

        # 步骤二：BM25 与向量路径并发且各自限时，
        # 单个异常只移除对应信号，不影响另一索引和基线。
        async def es_search() -> list[str]:
            index = MemoryElasticsearchIndex(ElasticsearchClient.get_client())
            return await index.search(
                query,
                source,
                categories,
                app_config.elasticsearch.top_k,
                user_id=user_id,
            )

        async def vector_search() -> list[str]:
            vector = await TEIEmbeddingClient.get_client().aembed_query(query)
            index = MemoryQdrantIndex(QdrantClient.get_client())
            return await index.search(
                vector,
                source,
                categories,
                app_config.qdrant.top_k,
                user_id=user_id,
            )

        results = await asyncio.gather(
            asyncio.wait_for(
                es_search(),
                timeout=app_config.memory.retrieval_timeout_seconds,
            ),
            asyncio.wait_for(
                vector_search(),
                timeout=app_config.memory.retrieval_timeout_seconds,
            ),
            return_exceptions=True,
        )
        for target, signal, result in (
            (MemoryIndexTarget.ELASTICSEARCH, "elasticsearch", results[0]),
            (MemoryIndexTarget.QDRANT, "qdrant", results[1]),
        ):
            if isinstance(result, BaseException):
                degraded.append(target)
                logger.bind(trace_id="-").warning(
                    "记忆检索降级 target={} error_type={}",
                    target.value,
                    type(result).__name__,
                )
            else:
                rankings.append((signal, result))

        # 步骤三：索引只提供候选 UID；
        # 统一回到同租户 MySQL 权威行，并读取各目标尚未收敛的 outbox 状态。
        candidate_uids = {
            *baseline_uids,
            *(uid for _signal, uids in rankings for uid in uids),
        }
        if not candidate_uids:
            return MemorySearchResponse(
                items=[],
                degraded_targets=degraded,
            )
        async with MySQLDatabase.session() as session:
            repository = MemoryRepository(session)
            memories = await repository.get_many_active(
                sorted(candidate_uids),
                user_id=user_id,
            )
            pending_targets = await MemoryIndexOutboxRepository(
                session
            ).pending_targets(candidate_uids)
        by_uid = {memory.detail.uid: memory.detail for memory in memories}
        target_by_signal = {
            "elasticsearch": MemoryIndexTarget.ELASTICSEARCH,
            "qdrant": MemoryIndexTarget.QDRANT,
        }
        # 步骤四：某目标仍有待处理的 desired state 时，其命中可能是旧投影，
        # 只剔除该目标的排名信号。
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
        # 步骤五：将精确基线与已确认信号做稳定 RRF；
        # 融合分数只决定候选顺序，不赋予内容权威性。
        fused = reciprocal_rank_fusion(
            [("mysql_exact", baseline_uids), *confirmed_rankings],
            constant=app_config.memory.rrf_constant,
            exact_uids=set(baseline_uids),
        )
        # 步骤六：返回前连续校验租户、状态、版本、内容哈希、有效期
        # 和对象白名单，拒绝所有陈旧命中。
        items: list[MemorySearchHit] = []
        for uid, score, signals in fused:
            detail = by_uid.get(uid)
            if detail is None:
                continue
            if (
                detail.source != source
                or detail.user_id != user_id
                or detail.status != MemoryStatus.ACTIVE
                or detail.content_version != app_config.memory.content_version
                or detail.projection_version != app_config.memory.projection_version
                or memory_content_hash(detail.content) != detail.content_hash
            ):
                continue
            if categories and detail.category not in categories:
                continue
            if detail.expires_at is not None and detail.expires_at <= datetime.now(
                UTC
            ).replace(tzinfo=None):
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
        # 步骤七：以尽力而为方式记录访问热度，统计失败不得撤销权威过滤结果。
        if items:
            try:
                async with MySQLDatabase.session() as session:
                    await MemoryRepository(session).record_access(
                        {item.memory.uid for item in items},
                        source=source,
                        user_id=user_id,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.bind(trace_id="-").warning(
                    "记忆访问统计写入失败，搜索结果已按 best-effort 返回 "
                    "source={} user_id={} item_count={} error_type={}",
                    source,
                    user_id,
                    len(items),
                    type(exc).__name__,
                )
        return MemorySearchResponse(items=items, degraded_targets=degraded)
