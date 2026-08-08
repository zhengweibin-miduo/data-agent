"""Query decisive EXPLAIN coordination against live MySQL Locking Service."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

import pytest

from conversation.models import (
    CompleteTurnResponse,
    ConversationContext,
    MessageRecord,
    MessageRole,
    StartTurnResponse,
)
from data_sync.locks import generation_lock_name
from infrastructure.generation_locks import GenerationLockManager
from infrastructure.mysql import AdvisoryLockUnavailableError
from models.jobs import DDLJobRequest
from models.physical import PhysicalSchema
from query.application.contracts import (
    ConversationPort,
    QueryBatch,
    QueryExecutorPort,
    QueryIntentPort,
    QueryMetadataPort,
    QueryPlannerPort,
    QueryReadinessPort,
    QueryRequest,
    SupplementalQueryContext,
)
from query.application.service import QueryApplication
from query.domain import (
    QueryContext,
    QueryDraft,
    QueryIntent,
    QueryType,
    SQLValidationIssue,
    ValidatedQuery,
)

pytestmark = pytest.mark.integration


def _message(role: MessageRole, content: str) -> MessageRecord:
    """Build one authoritative Conversation message for the public stream seam."""
    return MessageRecord(
        id=1 if role == MessageRole.USER else 2,
        uid=f"message-{role.value}",
        turn_uid="turn-generation",
        role=role,
        content=content,
        created_at=datetime.now(UTC),
    )


class _Conversations:
    """Provide an owned turn and persist the observable terminal summary."""

    async def start_turn(self, *_args: object, **_kwargs: object) -> StartTurnResponse:
        return StartTurnResponse(
            message=_message(MessageRole.USER, "查询销售额总和"),
            context=ConversationContext(messages=[], memories=[]),
            claim_token="c" * 32,
        )

    async def assistant_message(self, *_args: object) -> MessageRecord | None:
        return None

    async def pending_query_chain(self, *_args: object, **_kwargs: object):
        return [_message(MessageRole.USER, "查询销售额总和")]

    async def complete_turn(
        self, *_args: object, **_kwargs: object
    ) -> CompleteTurnResponse:
        return CompleteTurnResponse(
            message=_message(MessageRole.ASSISTANT, str(_args[-1]))
        )

    async def abandon_turn(self, *_args: object) -> None:
        return None

    async def renew_turn(self, *_args: object) -> bool:
        return True


class _Intent:
    """Return one exact-evidence aggregate intent."""

    async def parse(self, *_args: object) -> QueryIntent:
        return QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["销售额"],
        )


class _Metadata:
    """Bind the request's parsed schema and keep its authority stable."""

    async def build_context(
        self, question: str, intent: QueryIntent, schema: PhysicalSchema
    ) -> QueryContext:
        del question, intent
        return QueryContext(physical_schema=schema)

    async def relationships_are_authoritative(self, schema: PhysicalSchema) -> bool:
        del schema
        return True

    async def bindings_are_authoritative(self, context: QueryContext) -> bool:
        del context
        return True


class _Planner:
    """Generate one statically valid aggregate draft."""

    async def draft(
        self, context: QueryContext, intent: QueryIntent, trusted_time_range: object
    ) -> QueryDraft:
        del intent, trusted_time_range
        table = context.physical_schema.tables[0]
        amount = next(column for column in table.columns if column.name == "amount")
        return QueryDraft(
            sql="SELECT SUM(o.amount) AS total FROM dw.orders AS o",
            table_ids=[table.id],
            column_ids=[amount.id],
        )

    async def repair(
        self,
        context: QueryContext,
        intent: QueryIntent,
        trusted_time_range: object,
        draft: QueryDraft,
        issues: tuple[SQLValidationIssue, ...],
    ) -> QueryDraft:
        raise AssertionError(
            f"live coordination must not consume repair: {context} {intent} "
            f"{trusted_time_range} {draft} {issues}"
        )


class _Readiness:
    """Use the dedicated manager for the same target READ set as production."""

    def __init__(self, manager: GenerationLockManager) -> None:
        self._manager = manager
        self.ready_checks = 0

    async def ready(self, target_tables: tuple[str, ...]) -> bool:
        assert target_tables == ("orders",)
        self.ready_checks += 1
        return True

    @asynccontextmanager
    async def hold(self, target_tables: tuple[str, ...]):
        names = [generation_lock_name("dw", table) for table in target_tables]
        async with self._manager.read(names, 1):
            yield


class _Executor:
    """Attempt a live generation WRITE at each decisive EXPLAIN boundary."""

    def __init__(self, manager: GenerationLockManager) -> None:
        self._manager = manager
        self.explains = 0

    async def explain(self, query: ValidatedQuery) -> None:
        self.explains += 1
        name = generation_lock_name("dw", query.target_tables[0])
        with pytest.raises(AdvisoryLockUnavailableError):
            async with self._manager.write([name], 0):
                pytest.fail("decisive EXPLAIN ran without its generation READ owner")

    async def execute(self, query: ValidatedQuery):
        del query
        yield QueryBatch(columns=["total"], rows=[[1]])


async def test_decisive_explains_exclude_generation_write(
    generation_lock_manager: GenerationLockManager,
) -> None:
    """Both planning and final EXPLAIN retain a READ owner against live WRITE."""
    readiness = _Readiness(generation_lock_manager)
    executor = _Executor(generation_lock_manager)
    application = QueryApplication(
        conversations=cast(ConversationPort, _Conversations()),
        intents=cast(QueryIntentPort, _Intent()),
        metadata=cast(QueryMetadataPort, _Metadata()),
        planner=cast(QueryPlannerPort, _Planner()),
        readiness=cast(QueryReadinessPort, readiness),
        executor=cast(QueryExecutorPort, executor),
        dw_database="dw",
    )

    events = [
        event
        async for event in application.stream(
            QueryRequest(
                user_id="user-1",
                conversation_uid="conversation-1",
                turn_uid="turn-generation",
                question="查询销售额总和",
                supplemental_context=SupplementalQueryContext(
                    user_timezone="Asia/Shanghai"
                ),
                ddl_context=DDLJobRequest(
                    source="erp",
                    ddl=(
                        "CREATE TABLE orders (id BIGINT PRIMARY KEY, "
                        "amount DECIMAL(10,2))"
                    ),
                ),
            )
        )
    ]

    assert events[-1].kind == "complete"
    assert readiness.ready_checks == 2
    assert executor.explains == 2
