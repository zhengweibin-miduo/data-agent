"""模型输出的确定性元数据校验。"""

from collections import Counter

from data_agent.ddl_metadata.identifiers import metric_id
from data_agent.ddl_metadata.models.physical import PhysicalSchema
from data_agent.ddl_metadata.models.semantic import (
    ColumnRole,
    MetricAnswer,
    MetricMetadata,
    MetricQuestion,
    SemanticMetadata,
    TableRole,
    ValidationIssue,
)
from data_agent.settings import app_config


def _set_issues(
    actual: list[str],
    expected: set[str],
    path: str,
) -> list[ValidationIssue]:
    """生成对象集合或重复项问题。"""
    issues: list[ValidationIssue] = []
    counts = Counter(actual)
    duplicates = sorted(value for value, count in counts.items() if count > 1)
    unknown = sorted(set(actual) - expected)
    missing = sorted(expected - set(actual))
    if duplicates:
        issues.append(
            ValidationIssue(
                code="duplicate_object",
                path=path,
                message=f"重复对象: {','.join(duplicates)}",
            )
        )
    if unknown:
        issues.append(
            ValidationIssue(
                code="hallucinated_object",
                path=path,
                message=f"未知对象: {','.join(unknown)}",
            )
        )
    if missing:
        issues.append(
            ValidationIssue(
                code="missing_object",
                path=path,
                message=f"缺少对象: {','.join(missing)}",
            )
        )
    return issues


def validate_metadata(
    schema: PhysicalSchema,
    metadata: SemanticMetadata,
    confidence_threshold: float = app_config.llm.semantic_confidence_threshold,
) -> list[ValidationIssue]:
    """校验语义对象集合、结构角色、置信度和证据引用。"""
    expected_tables = {table.id for table in schema.tables}
    columns = {column.id: column for table in schema.tables for column in table.columns}
    expected_columns = set(columns)
    issues = _set_issues(
        [table.table_id for table in metadata.tables],
        expected_tables,
        "tables",
    )
    issues.extend(
        _set_issues(
            [column.column_id for column in metadata.columns],
            expected_columns,
            "columns",
        )
    )
    known_evidence = expected_tables | expected_columns
    for table in metadata.tables:
        if table.confidence < confidence_threshold:
            issues.append(
                ValidationIssue(
                    code="low_confidence",
                    path=f"tables.{table.table_id}.confidence",
                    message="表语义置信度不足",
                    repairable=False,
                )
            )
        if not table.evidence or not set(table.evidence) <= known_evidence:
            issues.append(
                ValidationIssue(
                    code="invalid_evidence",
                    path=f"tables.{table.table_id}.evidence",
                    message="表语义证据必须引用当前物理对象 ID",
                )
            )
    for column in metadata.columns:
        physical = columns.get(column.column_id)
        if physical is not None:
            expected_role = physical.structural_role
            if expected_role and column.role.value != expected_role:
                issues.append(
                    ValidationIssue(
                        code="structural_role_conflict",
                        path=f"columns.{column.column_id}.role",
                        message=f"结构角色必须为 {expected_role}",
                    )
                )
            if expected_role is None and column.role in {
                ColumnRole.PRIMARY_KEY,
                ColumnRole.FOREIGN_KEY,
            }:
                issues.append(
                    ValidationIssue(
                        code="invented_structural_role",
                        path=f"columns.{column.column_id}.role",
                        message="模型不能新增主键或外键角色",
                    )
                )
        if column.confidence < confidence_threshold:
            issues.append(
                ValidationIssue(
                    code="low_confidence",
                    path=f"columns.{column.column_id}.confidence",
                    message="列语义置信度不足",
                    repairable=False,
                )
            )
        if not column.evidence or not set(column.evidence) <= known_evidence:
            issues.append(
                ValidationIssue(
                    code="invalid_evidence",
                    path=f"columns.{column.column_id}.evidence",
                    message="列语义证据必须引用当前物理对象 ID",
                )
            )
    return issues


def validate_metric_questions(
    schema: PhysicalSchema,
    metadata: SemanticMetadata,
    questions: list[MetricQuestion],
) -> list[ValidationIssue]:
    """校验指标问题只引用当前事实表和列。"""
    fact_tables = {
        table.table_id for table in metadata.tables if table.role == TableRole.FACT
    }
    columns = {column.id for table in schema.tables for column in table.columns}
    issues = _set_issues(
        [question.question_id for question in questions],
        {question.question_id for question in questions},
        "questions",
    )
    for question in questions:
        if question.fact_table_id not in fact_tables:
            issues.append(
                ValidationIssue(
                    code="unknown_fact_table",
                    path=f"questions.{question.question_id}.fact_table_id",
                    message="问题必须引用当前事实表",
                )
            )
        unknown = set(question.column_ids) - columns
        if unknown:
            issues.append(
                ValidationIssue(
                    code="unknown_question_column",
                    path=f"questions.{question.question_id}.column_ids",
                    message=f"问题引用未知列: {','.join(sorted(unknown))}",
                )
            )
    return issues


def finalize_and_validate_metrics(
    source: str,
    schema: PhysicalSchema,
    metadata: SemanticMetadata,
    questions: list[MetricQuestion],
    answers: list[MetricAnswer],
    metrics: list[MetricMetadata],
) -> tuple[list[MetricMetadata], list[ValidationIssue]]:
    """分配稳定指标 ID 并验证引用、唯一性和回答依据。"""
    table_roles = {table.table_id: table.role for table in metadata.tables}
    column_ids = {column.id for table in schema.tables for column in table.columns}
    question_ids = {question.question_id for question in questions}
    answered_ids = {answer.question_id for answer in answers if answer.answer.strip()}
    finalized = [
        metric.model_copy(
            update={
                "id": metric_id(source, metric.fact_table_id, metric.name),
            }
        )
        for metric in metrics
    ]
    issues: list[ValidationIssue] = []
    names = [
        f"{metric.fact_table_id}\0{metric.name.strip().casefold()}"
        for metric in finalized
    ]
    duplicates = [name for name, count in Counter(names).items() if count > 1]
    if duplicates:
        issues.append(
            ValidationIssue(
                code="duplicate_metric",
                path="metrics",
                message="同一事实表存在重复指标名称",
            )
        )
    for metric in finalized:
        path = f"metrics.{metric.id}"
        if table_roles.get(metric.fact_table_id) != TableRole.FACT:
            issues.append(
                ValidationIssue(
                    code="unknown_metric_fact",
                    path=f"{path}.fact_table_id",
                    message="指标必须属于当前事实表",
                )
            )
        unknown_columns = set(metric.relevant_column_ids) - column_ids
        if unknown_columns:
            issues.append(
                ValidationIssue(
                    code="unknown_metric_column",
                    path=f"{path}.relevant_column_ids",
                    message=f"指标引用未知列: {','.join(sorted(unknown_columns))}",
                )
            )
        supports = set(metric.answer_question_ids)
        if not supports or not supports <= question_ids or not supports <= answered_ids:
            issues.append(
                ValidationIssue(
                    code="unsupported_metric_claim",
                    path=f"{path}.answer_question_ids",
                    message="指标必须仅引用已回答的当前问题",
                    repairable=False,
                )
            )
    return finalized, issues
