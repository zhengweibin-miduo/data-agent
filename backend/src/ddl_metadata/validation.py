"""模型输出的确定性元数据校验。"""

from collections import Counter

from identifiers import metric_id
from models.physical import PhysicalSchema
from models.semantic import (
    ColumnRole,
    MetricAnswer,
    MetricMetadata,
    MetricQuestion,
    SemanticMetadata,
    TableRole,
    ValidationIssue,
    ValueIndexDecision,
    ValueSensitivity,
)
from settings import app_config


def _set_issues(
    actual: list[str],
    expected: set[str],
    path: str,
) -> list[ValidationIssue]:
    """生成对象集合或重复项问题。"""
    # 步骤一：先计算重复、越界和缺失对象，统一形成集合差异。
    issues: list[ValidationIssue] = []
    counts = Counter(actual)
    duplicates = sorted(value for value, count in counts.items() if count > 1)
    unknown = sorted(set(actual) - expected)
    missing = sorted(expected - set(actual))
    # 步骤二：再把三类差异分别投影为稳定校验问题，供上层决定修复或拒绝。
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


def _value_index_evidence_by_column(schema: PhysicalSchema) -> dict[str, set[str]]:
    """建立每个字段可引用的值索引证据作用域。"""
    # 步骤一：当前字段与所属表始终属于字段自身上下文，并建立关系目标查找表。
    allowed = {
        column.id: {table.id, column.id}
        for table in schema.tables
        for column in table.columns
    }
    tables_by_name = {table.qualified_name.casefold(): table for table in schema.tables}
    source_tables = {table.id: table for table in schema.tables}
    # 步骤二：仅把当前字段直接引用的目标表列加入作用域，拒绝同模式无关对象。
    for relationship in schema.relationships:
        source_table = source_tables.get(relationship.source_table_id)
        target_name = relationship.target_table.casefold()
        target_table = tables_by_name.get(target_name)
        if target_table is None and source_table is not None and "." not in target_name:
            qualified_target = (
                f"{source_table.schema_name}.{target_name}"
                if source_table.schema_name
                else target_name
            )
            target_table = tables_by_name.get(qualified_target.casefold())
        if target_table is None:
            continue
        target_column = next(
            (
                column
                for column in target_table.columns
                if column.name.casefold() == relationship.target_column.casefold()
            ),
            None,
        )
        evidence = allowed.get(relationship.source_column_id)
        if evidence is not None:
            evidence.add(target_table.id)
            if target_column is not None:
                evidence.add(target_column.id)
    return allowed


def _foreign_key_neighbors_by_column(
    schema: PhysicalSchema,
) -> tuple[dict[str, set[str]], set[str]]:
    """按 MySQL 名称解析规则返回外键两端的直接相邻字段。"""
    neighbors: dict[str, set[str]] = {}
    unresolved_sources: set[str] = set()
    tables_by_name = {table.qualified_name.casefold(): table for table in schema.tables}
    source_tables = {table.id: table for table in schema.tables}
    for relationship in schema.relationships:
        source_table = source_tables.get(relationship.source_table_id)
        target_name = relationship.target_table.casefold()
        target_table = tables_by_name.get(target_name)
        if target_table is None and source_table is not None and "." not in target_name:
            schema_name = source_table.schema_name
            qualified = f"{schema_name}.{target_name}" if schema_name else target_name
            target_table = tables_by_name.get(qualified.casefold())
        if target_table is None:
            unresolved_sources.add(relationship.source_column_id)
            continue
        target_column = next(
            (
                column
                for column in target_table.columns
                if column.name.casefold() == relationship.target_column.casefold()
            ),
            None,
        )
        if target_column is not None:
            neighbors.setdefault(relationship.source_column_id, set()).add(
                target_column.id
            )
            neighbors.setdefault(target_column.id, set()).add(
                relationship.source_column_id
            )
        else:
            unresolved_sources.add(relationship.source_column_id)
    return neighbors, unresolved_sources


def validate_metadata(
    schema: PhysicalSchema,
    metadata: SemanticMetadata,
    confidence_threshold: float = app_config.llm.semantic_confidence_threshold,
) -> list[ValidationIssue]:
    """校验语义对象集合、结构角色、置信度和证据引用。"""
    # 步骤一：先以物理 Schema 为权威建立表列全集，并校验模型对象是否完整且唯一。
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
    # 步骤二：逐表校验置信度与证据引用，低置信度属于不可自动修复问题。
    known_evidence = expected_tables | expected_columns
    value_index_evidence = _value_index_evidence_by_column(schema)
    foreign_key_neighbors, unresolved_foreign_keys = _foreign_key_neighbors_by_column(
        schema
    )
    semantic_columns = {column.column_id: column for column in metadata.columns}
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
    # 步骤三：逐列对照解析器确定的结构角色，再校验置信度和证据边界。
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
        profile = column.value_index
        if not set(profile.evidence) <= value_index_evidence.get(
            column.column_id, set()
        ):
            issues.append(
                ValidationIssue(
                    code="invalid_value_index_evidence",
                    path=f"columns.{column.column_id}.value_index.evidence",
                    message="字段值索引证据必须引用当前字段、所属表或直接外键目标",
                )
            )
        if (
            profile.decision == ValueIndexDecision.INDEX
            and profile.sensitivity != ValueSensitivity.NON_SENSITIVE
        ):
            issues.append(
                ValidationIssue(
                    code="conflicting_value_index_profile",
                    path=f"columns.{column.column_id}.value_index",
                    message="只有明确非敏感字段可以获得值索引资格",
                )
            )
        if profile.eligible and any(
            semantic_columns[neighbor_id].value_index.sensitivity
            != ValueSensitivity.NON_SENSITIVE
            for neighbor_id in foreign_key_neighbors.get(column.column_id, set())
            if neighbor_id in semantic_columns
        ):
            issues.append(
                ValidationIssue(
                    code="conflicting_related_value_sensitivity",
                    path=f"columns.{column.column_id}.value_index.sensitivity",
                    message="直接外键任一端非明确非敏感时，另一端不能获得值索引资格",
                )
            )
        if profile.eligible and column.column_id in unresolved_foreign_keys:
            issues.append(
                ValidationIssue(
                    code="unverified_related_value_sensitivity",
                    path=f"columns.{column.column_id}.value_index.sensitivity",
                    message="无法核验外键目标敏感度时，引用字段不能获得值索引资格",
                )
            )
    return issues


def validate_metric_questions(
    schema: PhysicalSchema,
    metadata: SemanticMetadata,
    questions: list[MetricQuestion],
) -> list[ValidationIssue]:
    """校验指标问题只引用当前事实表和列。"""
    # 步骤一：从已验证语义和物理 Schema 建立允许引用的事实表与列集合。
    fact_tables = {
        table.table_id for table in metadata.tables if table.role == TableRole.FACT
    }
    columns = {column.id for table in schema.tables for column in table.columns}
    # 步骤二：先拒绝重复问题 ID，再逐题校验事实表和列引用。
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
    # 步骤一：建立当前表、列、问题与有效回答边界，并为模型指标重算稳定 ID。
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
    # 步骤二：在稳定 ID 投影后检查同一事实表内的规范化指标名称是否重复。
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
    # 步骤三：逐项验证事实表归属、列引用及已回答问题证据，避免无依据指标落库。
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
