"""MySQL 权威回查的 ES/Qdrant 混合记忆检索。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from loguru import logger

from data_agent.ddl_metadata.memory.indexes import (
    MemoryElasticsearchIndex,
    MemoryQdrantIndex,
)
from data_agent.ddl_metadata.memory.payloads import (
    content_object_ids,
    memory_content_hash,
)
from data_agent.ddl_metadata.models import (
    MemoryIndexTarget,
    MemoryKind,
    MemorySearchHit,
    MemorySearchResponse,
    MemoryStatus,
)
from data_agent.ddl_metadata.persistence.memory_repository import MemoryRepository
from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.infrastructure.tei_embeddings import TEIEmbeddingClient
from data_agent.settings import app_config


def reciprocal_rank_fusion(
    rankings: Sequence[tuple[str, Sequence[str]]],
    *,
    constant: int,
    exact_uids: set[str] | None = None,
) -> list[tuple[str, float, list[str]]]:
    """按稳定 UID tie-break 融合不同分数量纲的排名。"""
    scores: dict[str, float] = {}
    signals: dict[str, set[str]] = {}
    for signal, uids in rankings:
        for rank, uid in enumerate(uids, start=1):
            scores[uid] = scores.get(uid, 0.0) + 1 / (constant + rank)
            signals.setdefault(uid, set()).add(signal)
    for uid in exact_uids or set():
        scores[uid] = scores.get(uid, 0.0) + 1.0
        signals.setdefault(uid, set()).add("exact")
    return [
        (uid, score, sorted(signals[uid]))
        for uid, score in sorted(
            scores.items(),
            key=lambda item: (-item[1], item[0]),
        )
    ]


class MemorySearchService:
    """并发检索派生索引并仅返回复核后的 MySQL 权威内容。"""

    async def search(
        self,
        query: str,
        source: str,
        *,
        kinds: set[MemoryKind] | None = None,
        limit: int | None = None,
        exact_uids: Sequence[str] = (),
        allowed_object_ids: set[str] | None = None,
    ) -> MemorySearchResponse:
        """执行稳定 RRF，并在索引失败时安全降级。"""
        bounded_limit = min(
            limit or app_config.memory.search_limit,
            app_config.memory.search_limit,
        )
        rankings: list[tuple[str, Sequence[str]]] = []
        degraded: list[MemoryIndexTarget] = []
        if exact_uids:
            baseline_uids = list(exact_uids)
        else:
            async with MySQLDatabase.session() as session:
                baseline_uids = await MemoryRepository(session).find_exact_query(
                    source,
                    query,
                    kinds,
                    limit=bounded_limit,
                )

        async def es_search() -> list[str]:
            index = MemoryElasticsearchIndex(ElasticsearchClient.get_client())
            return await index.search(
                query,
                source,
                kinds,
                app_config.elasticsearch.top_k,
            )

        async def vector_search() -> list[str]:
            vector = await TEIEmbeddingClient.get_client().aembed_query(query)
            index = MemoryQdrantIndex(QdrantClient.get_client())
            return await index.search(
                vector,
                source,
                kinds,
                app_config.qdrant.top_k,
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
            memories = await repository.get_many_active(sorted(candidate_uids))
            pending_targets = await repository.pending_outbox_targets(candidate_uids)
        by_uid = {memory.detail.uid: memory.detail for memory in memories}
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
                    if target_by_signal[signal]
                    not in pending_targets.get(uid, set())
                ],
            )
            for signal, uids in rankings
        ]
        fused = reciprocal_rank_fusion(
            confirmed_rankings,
            constant=app_config.memory.rrf_constant,
            exact_uids=set(baseline_uids),
        )
        items: list[MemorySearchHit] = []
        for uid, score, signals in fused:
            detail = by_uid.get(uid)
            if detail is None:
                continue
            if (
                detail.source != source
                or detail.status != MemoryStatus.ACTIVE
                or detail.content_version != app_config.memory.content_version
                or detail.projection_version != app_config.memory.projection_version
                or memory_content_hash(detail.content) != detail.content_hash
            ):
                continue
            if kinds and detail.kind not in kinds:
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
                    score=score,
                    signals=signals,
                )
            )
            if len(items) >= bounded_limit:
                break
        return MemorySearchResponse(items=items, degraded_targets=degraded)
