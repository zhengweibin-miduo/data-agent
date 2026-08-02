"""精确命中加成与排名项分工的量纲契约测试。"""

from __future__ import annotations

from memory.domain.ranking import reciprocal_rank_fusion
from tests.helpers.checks import check_condition, check_equal


def test_exact_bonus_dominates_semantic_signals() -> None:
    """精确命中必须整体排在纯语义命中之前，且加成量纲压倒 RRF 项。"""
    fused = reciprocal_rank_fusion(
        [
            ("mysql_exact", ["exact-1"]),
            # 语义信号把另一个候选排在所有信号的第一位。
            ("elasticsearch", ["semantic-1", "exact-1"]),
            ("qdrant", ["semantic-1", "exact-1"]),
        ],
        constant=60,
        exact_uids={"exact-1"},
    )
    order = [uid for uid, _score, _signals in fused]
    check_equal("精确命中排在语义命中之前", order, ["exact-1", "semantic-1"])
    scores = {uid: score for uid, score, _signals in fused}
    check_condition(
        "加成量纲压倒单个 RRF 项",
        scores["exact-1"] - scores["semantic-1"] > 0.5,
        actual=scores,
        expected="精确命中分差远大于单个 RRF 项（约 0.016）",
    )


def test_ranking_entry_orders_exact_hits_among_themselves() -> None:
    """精确命中之间的顺序由排名项决定，而不是退化为 UID 字符串序。"""
    # 基线把 b 排在 a 之前；若只传 exact_uids，两者同分后会按 UID 升序变成 a、b。
    fused = reciprocal_rank_fusion(
        [("mysql_exact", ["b", "a"])],
        constant=60,
        exact_uids={"a", "b"},
    )
    check_equal(
        "保留基线给出的相关性顺序",
        [uid for uid, _score, _signals in fused],
        ["b", "a"],
    )

    # 去掉排名项后同分，只能按 UID 升序——这正是两份贡献缺一不可的原因。
    degraded = reciprocal_rank_fusion([], constant=60, exact_uids={"a", "b"})
    check_equal(
        "缺少排名项时退化为 UID 序",
        [uid for uid, _score, _signals in degraded],
        ["a", "b"],
    )


def test_exact_signal_is_reported_alongside_ranking_signals() -> None:
    """精确命中同时携带 exact 与其排名信号，便于调用方解释来源。"""
    fused = reciprocal_rank_fusion(
        [("mysql_exact", ["a"]), ("elasticsearch", ["a"])],
        constant=60,
        exact_uids={"a"},
    )
    check_equal(
        "信号集合包含全部来源",
        fused[0][2],
        ["elasticsearch", "exact", "mysql_exact"],
    )
