"""DDL 元数据仓储检查的安全测试数据助手。"""

from sqlalchemy import delete, or_, select

from data_agent.ddl_metadata.persistence.tables import (
    column_info,
    column_metric,
    metric_info,
    table_info,
)
from data_agent.identifiers import metric_id
from data_agent.infrastructure.mysql import MySQLDatabase
from data_agent.memory.mysql.tables import (
    agent_memory,
    agent_memory_event,
    agent_memory_link,
    memory_index_outbox,
)
from data_agent.models.physical import PhysicalSchema
from data_agent.models.semantic import (
    ColumnRole,
    MetricAnswer,
    MetricMetadata,
    MetricQuestion,
    SemanticColumn,
    SemanticMetadata,
    SemanticTable,
    TableRole,
)
from data_agent.persistence.schema import metadata


async def ensure_schema() -> None:
    """在 Meta 与配置的应用库中仅创建缺失测试表。"""
    engine = MySQLDatabase.initialize()
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)


def semantic_for(
    schema: PhysicalSchema,
    *,
    fact: bool,
) -> SemanticMetadata:
    """构造与 AST 完全对齐的确定性语义结果。"""
    tables = [
        SemanticTable(
            table_id=table.id,
            role=TableRole.FACT if fact else TableRole.DIM,
            description=f"{table.name} accepted description",
            confidence=0.99,
            evidence=[table.id],
        )
        for table in schema.tables
    ]
    columns = [
        SemanticColumn(
            column_id=column.id,
            role=(
                ColumnRole(column.structural_role)
                if column.structural_role
                else (
                    ColumnRole.MEASURE
                    if fact and column.name == "amount"
                    else ColumnRole.DIMENSION
                )
            ),
            description=f"{column.name} accepted description",
            confidence=0.99,
            evidence=[column.id],
        )
        for table in schema.tables
        for column in table.columns
    ]
    return SemanticMetadata(tables=tables, columns=columns)


def metric_bundle(
    schema: PhysicalSchema,
) -> tuple[list[MetricQuestion], list[MetricAnswer], list[MetricMetadata]]:
    """构造一个完整且可追踪到回答的指标。"""
    fact_id = schema.tables[0].id
    amount_id = next(
        column.id for column in schema.tables[0].columns if column.name == "amount"
    )
    question = MetricQuestion(
        question_id="total_amount.definition",
        prompt="Define total amount",
        fact_table_id=fact_id,
        column_ids=[amount_id],
    )
    answer = MetricAnswer(
        question_id=question.question_id,
        answer="SUM(amount), all rows, yuan",
    )
    metric = MetricMetadata(
        id=metric_id(schema.source, fact_id, "total_amount"),
        name="total_amount",
        fact_table_id=fact_id,
        definition=answer.answer,
        relevant_column_ids=[amount_id],
        answer_question_ids=[question.question_id],
    )
    return [question], [answer], [metric]


async def cleanup_schema(schema: PhysicalSchema) -> None:
    """在同一事务中按明确 ID 清理 Meta 快照和应用记忆。"""
    table_ids = {table.id for table in schema.tables}
    column_ids = {column.id for table in schema.tables for column in table.columns}
    async with MySQLDatabase.session() as session:
        memory_ids = set(
            (
                await session.scalars(
                    select(agent_memory.c.id).where(
                        agent_memory.c.source == schema.source
                    )
                )
            ).all()
        )
        if memory_ids:
            await session.execute(
                delete(agent_memory_link).where(
                    or_(
                        agent_memory_link.c.memory_id.in_(memory_ids),
                        agent_memory_link.c.linked_memory_id.in_(memory_ids),
                    )
                )
            )
            await session.execute(
                delete(agent_memory_event).where(
                    agent_memory_event.c.memory_id.in_(memory_ids)
                )
            )
            await session.execute(
                delete(memory_index_outbox).where(
                    memory_index_outbox.c.memory_uid.in_(
                        select(agent_memory.c.uid).where(
                            agent_memory.c.id.in_(memory_ids)
                        )
                    )
                )
            )
            await session.execute(
                delete(agent_memory).where(agent_memory.c.id.in_(memory_ids))
            )
        metric_ids = set()
        if column_ids:
            metric_ids.update(
                (
                    await session.scalars(
                        select(column_metric.c.metric_id).where(
                            column_metric.c.column_id.in_(column_ids)
                        )
                    )
                ).all()
            )
            await session.execute(
                delete(column_metric).where(column_metric.c.column_id.in_(column_ids))
            )
            await session.execute(
                delete(column_info).where(column_info.c.id.in_(column_ids))
            )
        if metric_ids:
            await session.execute(
                delete(metric_info).where(metric_info.c.id.in_(metric_ids))
            )
        if table_ids:
            await session.execute(
                delete(table_info).where(table_info.c.id.in_(table_ids))
            )
