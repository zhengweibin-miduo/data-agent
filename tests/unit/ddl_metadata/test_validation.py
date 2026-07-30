"""模型元数据确定性校验检查。"""

import pytest
from pydantic import ValidationError

from data_agent.ddl_metadata.parsing import parse_ddl
from data_agent.ddl_metadata.validation import (
    finalize_and_validate_metrics,
    validate_metadata,
    validate_metric_questions,
)
from data_agent.models.physical import PhysicalSchema
from data_agent.models.semantic import (
    ColumnRole,
    ColumnValueIndexProfile,
    MetricAnswer,
    MetricMetadata,
    MetricQuestion,
    SemanticColumn,
    SemanticMetadata,
    SemanticTable,
    TableRole,
    ValueIndexDecision,
    ValueSensitivity,
)
from tests.helpers.checks import check_condition, check_equal

DDL = """
CREATE TABLE dim_customer (id BIGINT PRIMARY KEY, name VARCHAR(100));
CREATE TABLE fact_order (
    id BIGINT PRIMARY KEY,
    customer_id BIGINT,
    amount DECIMAL(10,2),
    FOREIGN KEY (customer_id) REFERENCES dim_customer(id)
);
"""


async def _valid_metadata() -> tuple[PhysicalSchema, SemanticMetadata]:
    """构造与解析模式严格对齐的语义结果。"""
    schema = await parse_ddl("validator", DDL)
    semantic_tables = []
    semantic_columns = []
    for table in schema.tables:
        semantic_tables.append(
            SemanticTable(
                table_id=table.id,
                role=(TableRole.FACT if table.name == "fact_order" else TableRole.DIM),
                description=f"{table.name} description",
                confidence=0.99,
                evidence=[table.id],
            )
        )
        for column in table.columns:
            role = (
                ColumnRole(column.structural_role)
                if column.structural_role
                else (
                    ColumnRole.MEASURE
                    if column.name == "amount"
                    else ColumnRole.DIMENSION
                )
            )
            semantic_columns.append(
                SemanticColumn(
                    column_id=column.id,
                    role=role,
                    description=f"{column.name} description",
                    confidence=0.99,
                    evidence=[column.id],
                    value_index=ColumnValueIndexProfile(
                        decision=ValueIndexDecision.INDEX,
                        sensitivity=ValueSensitivity.NON_SENSITIVE,
                        reason="字段值可用于业务检索",
                        evidence=[table.id, column.id],
                    ),
                )
            )
    return schema, SemanticMetadata(
        tables=semantic_tables,
        columns=semantic_columns,
    )


async def test_metadata_validator() -> None:
    """覆盖成功、幻觉、结构角色、置信度和指标引用。"""
    schema, metadata = await _valid_metadata()
    check_equal(
        "test_metadata_validator 检查点 1",
        validate_metadata(schema, metadata),
        [],
    )

    first_column = metadata.columns[0]
    invalid = metadata.model_copy(
        update={
            "columns": [
                first_column.model_copy(
                    update={
                        "column_id": "unknown",
                        "role": ColumnRole.MEASURE,
                        "confidence": 0.1,
                    }
                ),
                *metadata.columns[1:],
            ]
        }
    )
    codes = {issue.code for issue in validate_metadata(schema, invalid)}
    check_condition(
        "test_metadata_validator 检查点 2",
        {
            "hallucinated_object",
            "missing_object",
            "low_confidence",
        }
        <= codes,
        expected="原断言条件成立",
    )

    fact_id = next(
        table.table_id for table in metadata.tables if table.role == TableRole.FACT
    )
    amount_id = next(
        column.id
        for table in schema.tables
        for column in table.columns
        if column.name == "amount"
    )
    question = MetricQuestion(
        question_id="average_amount.definition",
        prompt="How is average amount calculated?",
        fact_table_id=fact_id,
        column_ids=[amount_id],
    )
    check_equal(
        "test_metadata_validator 检查点 3",
        validate_metric_questions(schema, metadata, [question]),
        [],
    )
    answer = MetricAnswer(
        question_id=question.question_id,
        answer="SUM(amount) / COUNT(DISTINCT id), all orders, yuan",
    )
    metric = MetricMetadata(
        name="average_order_amount",
        fact_table_id=fact_id,
        definition=answer.answer,
        relevant_column_ids=[amount_id],
        answer_question_ids=[question.question_id],
    )
    finalized, issues = finalize_and_validate_metrics(
        "validator",
        schema,
        metadata,
        [question],
        [answer],
        [metric],
    )
    check_equal("test_metadata_validator 检查点 4", issues, [])
    check_equal("test_metadata_validator 检查点 5", len(finalized[0].id), 64)

    unsupported = metric.model_copy(update={"answer_question_ids": ["not_answered"]})
    _, issues = finalize_and_validate_metrics(
        "validator",
        schema,
        metadata,
        [question],
        [answer],
        [unsupported],
    )
    check_equal(
        "test_metadata_validator 检查点 6",
        {issue.code for issue in issues},
        {"unsupported_metric_claim"},
    )


async def test_value_index_profile_uses_strict_three_state_gate() -> None:
    """只有 index + non_sensitive + 当前证据通过值索引门禁。"""
    schema, metadata = await _valid_metadata()
    first = metadata.columns[0]

    for decision, sensitivity in (
        (ValueIndexDecision.SKIP, ValueSensitivity.SENSITIVE),
        (ValueIndexDecision.UNKNOWN, ValueSensitivity.UNKNOWN),
    ):
        profile = first.value_index.model_copy(
            update={"decision": decision, "sensitivity": sensitivity}
        )
        candidate = metadata.model_copy(
            update={
                "columns": [
                    first.model_copy(update={"value_index": profile}),
                    *metadata.columns[1:],
                ]
            }
        )
        check_equal("非索引三态仍是合法事实", validate_metadata(schema, candidate), [])

    conflict = first.value_index.model_copy(
        update={"sensitivity": ValueSensitivity.SENSITIVE}
    )
    invalid_evidence = first.value_index.model_copy(update={"evidence": ["missing"]})
    candidate = metadata.model_copy(
        update={
            "columns": [
                first.model_copy(update={"value_index": conflict}),
                metadata.columns[1].model_copy(
                    update={"value_index": invalid_evidence}
                ),
                *metadata.columns[2:],
            ]
        }
    )
    check_equal(
        "冲突与越界证据均拒绝",
        {issue.code for issue in validate_metadata(schema, candidate)},
        {"conflicting_value_index_profile", "invalid_value_index_evidence"},
    )
    with pytest.raises(ValidationError):
        ColumnValueIndexProfile(
            decision=ValueIndexDecision.INDEX,
            sensitivity=ValueSensitivity.NON_SENSITIVE,
            reason="",
            evidence=[],
        )


async def test_value_index_evidence_is_scoped_to_column_context() -> None:
    """值索引证据只允许当前字段上下文及其直接外键目标。"""
    schema, metadata = await _valid_metadata()
    customer_table = next(
        table for table in schema.tables if table.name == "dim_customer"
    )
    order_table = next(table for table in schema.tables if table.name == "fact_order")
    customer_id = next(
        column.id for column in customer_table.columns if column.name == "id"
    )
    foreign_key = next(
        column for column in metadata.columns if column.column_id in {
            physical.id
            for physical in order_table.columns
            if physical.name == "customer_id"
        }
    )
    unrelated = next(
        column for column in metadata.columns if column.column_id in {
            physical.id
            for physical in order_table.columns
            if physical.name == "amount"
        }
    )

    related_profile = foreign_key.value_index.model_copy(
        update={"evidence": [foreign_key.column_id, customer_table.id, customer_id]}
    )
    unrelated_profile = unrelated.value_index.model_copy(
        update={"evidence": [customer_table.id]}
    )
    candidate = metadata.model_copy(
        update={
            "columns": [
                column.model_copy(
                    update={
                        "value_index": (
                            related_profile
                            if column.column_id == foreign_key.column_id
                            else unrelated_profile
                        )
                    }
                )
                if column.column_id in {foreign_key.column_id, unrelated.column_id}
                else column
                for column in metadata.columns
            ]
        }
    )

    issues = validate_metadata(schema, candidate)
    check_equal(
        "直接外键目标通过且无关对象被拒绝",
        [
            issue.path
            for issue in issues
            if issue.code == "invalid_value_index_evidence"
        ],
        [f"columns.{unrelated.column_id}.value_index.evidence"],
    )
