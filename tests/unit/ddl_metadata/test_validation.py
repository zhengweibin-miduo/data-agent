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
    relationship_source = schema.relationships[0].source_column_id
    first = next(
        column for column in metadata.columns if column.column_id == relationship_source
    )

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
                    *[
                        column.model_copy(update={"value_index": profile})
                        if column.column_id == first.column_id
                        else column
                        for column in metadata.columns
                    ],
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
                *[
                    column.model_copy(update={"value_index": conflict})
                    if column.column_id == first.column_id
                    else column.model_copy(update={"value_index": invalid_evidence})
                    if column.column_id == metadata.columns[1].column_id
                    else column
                    for column in metadata.columns
                ],
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
        column
        for column in metadata.columns
        if column.column_id
        in {
            physical.id
            for physical in order_table.columns
            if physical.name == "customer_id"
        }
    )
    unrelated = next(
        column
        for column in metadata.columns
        if column.column_id
        in {
            physical.id for physical in order_table.columns if physical.name == "amount"
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


async def test_value_index_evidence_resolves_mysql_reference_names() -> None:
    """限定建表名下未限定且大小写不同的外键目标仍属于真实关系证据。"""
    schema = await parse_ddl(
        "validator",
        """
        CREATE TABLE sales.dim_customer (ID BIGINT PRIMARY KEY);
        CREATE TABLE sales.fact_order (
            order_id BIGINT PRIMARY KEY,
            customer_id BIGINT,
            FOREIGN KEY (customer_id) REFERENCES DIM_CUSTOMER(id)
        );
        """,
    )
    target = next(table for table in schema.tables if table.name == "dim_customer")
    source = next(table for table in schema.tables if table.name == "fact_order")
    source_column = next(
        column for column in source.columns if column.name == "customer_id"
    )
    target_column = target.columns[0]
    metadata = SemanticMetadata(
        tables=[
            SemanticTable(
                table_id=table.id,
                role=TableRole.DIM,
                description=table.name,
                confidence=0.99,
                evidence=[table.id],
            )
            for table in schema.tables
        ],
        columns=[
            SemanticColumn(
                column_id=column.id,
                role=(
                    ColumnRole(column.structural_role)
                    if column.structural_role
                    else ColumnRole.DIMENSION
                ),
                description=column.name,
                confidence=0.99,
                evidence=[column.id],
                value_index=ColumnValueIndexProfile(
                    decision=ValueIndexDecision.INDEX,
                    sensitivity=ValueSensitivity.NON_SENSITIVE,
                    reason="真实外键关系支持业务检索",
                    evidence=(
                        [column.id, target.id, target_column.id]
                        if column.id == source_column.id
                        else [column.id]
                    ),
                ),
            )
            for table in schema.tables
            for column in table.columns
        ],
    )

    check_equal(
        "未限定外键目标按来源 schema 解析",
        validate_metadata(schema, metadata),
        [],
    )


async def test_foreign_key_cannot_index_sensitive_target_values() -> None:
    """直接外键目标非明确非敏感时，引用端不得侧写相同标识值。"""
    schema, metadata = await _valid_metadata()
    relationship = schema.relationships[0]
    source_table = next(
        table for table in schema.tables if table.id == relationship.source_table_id
    )
    target_name = relationship.target_table.casefold()
    if "." not in target_name and source_table.schema_name:
        target_name = f"{source_table.schema_name}.{target_name}"
    target_table = next(
        table
        for table in schema.tables
        if table.qualified_name.casefold() == target_name
    )
    target_id = next(
        column.id
        for column in target_table.columns
        if column.name.casefold() == relationship.target_column.casefold()
    )
    candidate = metadata.model_copy(
        update={
            "columns": [
                column.model_copy(
                    update={
                        "value_index": column.value_index.model_copy(
                            update={"sensitivity": ValueSensitivity.SENSITIVE}
                        )
                    }
                )
                if column.column_id == target_id
                else column
                for column in metadata.columns
            ]
        }
    )

    check_equal(
        "敏感目标阻止引用端值索引",
        "conflicting_related_value_sensitivity"
        in {issue.code for issue in validate_metadata(schema, candidate)},
        True,
    )
