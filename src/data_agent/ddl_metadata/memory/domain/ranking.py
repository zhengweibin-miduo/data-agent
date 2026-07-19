"""记忆候选排名融合。"""

from collections.abc import Sequence


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
