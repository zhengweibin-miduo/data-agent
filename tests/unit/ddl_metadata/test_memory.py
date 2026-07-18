"""Mem0 风格记忆纯函数检查。"""

from data_agent.ddl_metadata.memory.payloads import (
    build_memory_text,
    memory_content_hash,
)
from data_agent.ddl_metadata.memory.search import reciprocal_rank_fusion
from data_agent.ddl_metadata.models import MemoryKind, SemanticDecisionContent
from data_agent.ddl_metadata.parsing import parse_ddl
from tests.helpers.checks import check_condition, check_equal
from tests.helpers.factories import semantic_for


def test_memory_projection_and_rrf() -> None:
    """验证确定性文本、哈希及稳定 RRF。"""
    schema = parse_ddl(
        "unit_memory",
        "CREATE TABLE dim_customer (id BIGINT PRIMARY KEY, name VARCHAR(64))",
    )
    content = SemanticDecisionContent(
        kind=MemoryKind.SEMANTIC_DECISION,
        table=semantic_for(schema, fact=False).tables[0],
    )
    check_equal(
        "test_memory_projection_and_rrf 检查点 1",
        memory_content_hash(content),
        memory_content_hash(content.model_copy()),
    )
    text = build_memory_text(content)
    check_condition(
        "test_memory_projection_and_rrf 检查点 2",
        schema.tables[0].id in text,
        expected="检索文本包含稳定对象 ID",
    )
    fused = reciprocal_rank_fusion(
        [
            ("elasticsearch", ["b", "a"]),
            ("qdrant", ["a", "b"]),
        ],
        constant=60,
        exact_uids={"b"},
    )
    check_equal(
        "test_memory_projection_and_rrf 检查点 3",
        [uid for uid, _score, _signals in fused],
        ["b", "a"],
    )
