"""记忆候选排名融合。"""

from collections.abc import Sequence


def reciprocal_rank_fusion(
    rankings: Sequence[tuple[str, Sequence[str]]],
    *,
    constant: int,
    exact_uids: set[str] | None = None,
) -> list[tuple[str, float, list[str]]]:
    """按稳定 UID 打破并列，融合不同分数量纲的排名。

    量纲差异是刻意设计的，不是重复计分：单个排名项的贡献是
    ``1 / (constant + rank)``（constant=60 时约 0.016），而 ``exact_uids`` 的加成
    是 1.0，因此精确命中必然整体排在纯语义命中之前——这是"精确优先"的产品意图。

    调用方通常把同一份精确命中列表既作为一个排名信号传入，又通过 ``exact_uids``
    传入。两份贡献分工不同、缺一不可：1.0 的加成决定精确命中相对其它信号的位置，
    排名项决定精确命中**彼此之间**的顺序。只传 ``exact_uids`` 会让所有精确命中同分，
    最终退化成按 UID 字符串排序，丢失基线查询给出的相关性顺序。

    融合分数只决定候选顺序，不赋予任何内容权威性；权威性由调用方在返回前对 MySQL
    权威行做连续校验保证。

    Args:
        rankings: 每个检索信号的候选 UID 排名，按相关性降序。
        constant: RRF 常量，抑制头部排名的过大权重。
        exact_uids: 需要整体优先的精确命中集合。

    Returns:
        按融合分数降序、同分按 UID 升序的 ``(uid, score, signals)`` 列表。
    """
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
