"""Mem0 风格记忆纯函数检查。"""

from data_agent.ddl_metadata.memory.domain.candidates import build_accepted_memories
from data_agent.ddl_metadata.memory.domain.lifecycle import (
    decide_memory,
    semantically_equivalent,
)
from data_agent.ddl_metadata.memory.domain.payloads import (
    build_memory_text,
    memory_content_hash,
)
from data_agent.ddl_metadata.memory.domain.ranking import reciprocal_rank_fusion
from data_agent.ddl_metadata.memory.indexing.rebuilder import MemoryIndexRebuilder
from data_agent.ddl_metadata.models.memory import (
    BuiltinMemoryCategory,
    MemoryDecision,
    MetricDefinitionContent,
    SemanticDecisionContent,
    UserMemoryContent,
)
from data_agent.ddl_metadata.parsing import parse_ddl
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)
from tests.helpers.factories import metric_bundle, semantic_for


async def test_memory_projection_and_rrf() -> None:
    """验证确定性文本、哈希及稳定 RRF。"""
    schema = await parse_ddl(
        "unit_memory",
        "CREATE TABLE dim_customer (id BIGINT PRIMARY KEY, name VARCHAR(64))",
    )
    content = SemanticDecisionContent(
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


def test_memory_lifecycle_decisions() -> None:
    """验证五种写入决策的确定性边界。"""
    cases = {
        "add": (decide_memory(has_active=False), MemoryDecision.ADD),
        "update": (decide_memory(has_active=True), MemoryDecision.UPDATE),
        "merge": (
            decide_memory(has_active=True, merge_requested=True),
            MemoryDecision.MERGE,
        ),
        "delete": (
            decide_memory(has_active=True, delete_requested=True),
            MemoryDecision.DELETE,
        ),
        "noop": (
            decide_memory(has_active=True, same_content=True),
            MemoryDecision.NOOP,
        ),
        "delete_missing": (
            decide_memory(has_active=False, delete_requested=True),
            MemoryDecision.NOOP,
        ),
    }
    for label, (actual, expected) in cases.items():
        check_equal(f"记忆生命周期决策 {label}", actual, expected)


def test_user_memory_semantic_duplicate_ignores_new_evidence() -> None:
    """相同规范事实换一条证据消息仍是语义重复。"""
    current = UserMemoryContent(
        value="Only metric units",
        supporting_user_quote="I use Only metric units",
        evidence_message_uids=["message-a"],
    )
    repeated = UserMemoryContent(
        value="  only   METRIC units ",
        supporting_user_quote="Again: only metric units",
        evidence_message_uids=["message-b"],
    )
    check_condition(
        "用户长期记忆语义去重",
        semantically_equivalent(current, repeated),
        expected="规范化 value 相同",
    )


async def test_metric_questions_are_evidence_not_memory_rows() -> None:
    """指标问答只随最终指标保存，不独立提升为长期记忆。"""
    schema = await parse_ddl(
        "unit_metric_memory",
        "CREATE TABLE fact_order (amount DECIMAL(18,2))",
    )
    questions, answers, metrics = metric_bundle(schema)
    candidates = build_accepted_memories(
        schema,
        semantic_for(schema, fact=True),
        questions,
        answers,
        metrics,
    )
    check_equal(
        "长期记忆类别不包含原始问答",
        {candidate.category for candidate in candidates},
        {
            BuiltinMemoryCategory.DDL_SEMANTIC.value,
            BuiltinMemoryCategory.DDL_METRIC.value,
        },
    )
    metric_content = next(
        candidate.content
        for candidate in candidates
        if candidate.category == BuiltinMemoryCategory.DDL_METRIC.value
    )
    check_equal(
        "指标内容保留问题证据",
        metric_content.questions
        if isinstance(metric_content, MetricDefinitionContent)
        else [],
        questions,
    )
    check_equal(
        "指标内容保留回答证据",
        metric_content.answers
        if isinstance(metric_content, MetricDefinitionContent)
        else [],
        answers,
    )


async def test_index_rebuild_requires_exact_target_confirmation() -> None:
    """验证派生索引删除前必须逐字确认目标。"""
    try:
        await MemoryIndexRebuilder().reset_indexes(
            confirmed_es_index="wrong",
            confirmed_qdrant_collection="wrong",
        )
    except BaseException as error:
        check_exception("索引重建目标保护", error, ValueError)
    else:
        fail_check(
            "索引重建目标保护",
            actual="未拒绝错误目标",
            expected="ValueError",
        )
