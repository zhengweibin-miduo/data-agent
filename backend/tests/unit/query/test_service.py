"""自然语言查询应用 seam 的行为测试。"""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from langchain_core.language_models.chat_models import BaseChatModel

from conversation.models import (
    CompleteTurnResponse,
    ContextMessage,
    ConversationContext,
    MessageRecord,
    MessageRole,
    StartTurnResponse,
)
from errors import DataAgentError
from infrastructure.mysql import AdvisoryLockReleaseError
from models.jobs import DDLJobRequest
from models.physical import PhysicalSchema
from query.adapters.llm import QueryLLMAdapter
from query.application.contracts import (
    ConversationPort,
    QueryBatch,
    QueryClarification,
    QueryExecutorPort,
    QueryExplainRejected,
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

_SUPPLEMENTAL_CONTEXT = SupplementalQueryContext(user_timezone="Asia/Shanghai")


def _message(role: MessageRole, content: str) -> MessageRecord:
    """构造一条查询轮次消息。"""
    return MessageRecord(
        id=1 if role == MessageRole.USER else 2,
        uid=f"message-{role.value}",
        turn_uid="turn-1",
        role=role,
        content=content,
        created_at=datetime.now(UTC),
    )


class _Conversations:
    """记录 Query Application 可观察到的 Conversation 完成结果。"""

    def __init__(self) -> None:
        self.completed: list[str] = []
        self.abandoned = 0
        self.semantic_fingerprints: list[str | None] = []
        self.chain = [_message(MessageRole.USER, "查询销售额总和")]

    async def start_turn(
        self,
        *_args: object,
        semantic_fingerprint: str | None = None,
    ) -> StartTurnResponse:
        """返回包含当前用户原文的已开始轮次。"""
        self.semantic_fingerprints.append(semantic_fingerprint)
        content = "查询销售额总和"
        return StartTurnResponse(
            message=_message(MessageRole.USER, content),
            context=ConversationContext(
                messages=[ContextMessage(role=MessageRole.USER, content=content)],
                memories=[],
            ),
            claim_token="c" * 32,
        )

    async def assistant_message(self, *_args: object) -> MessageRecord | None:
        """表示当前轮次尚未完成。"""
        return None

    async def pending_query_chain(self, *_args: object, **_kwargs: object):
        """返回权威 Query 澄清链替身。"""
        return self.chain

    async def complete_turn(
        self, *_args: object, semantic_fingerprint: str | None = None
    ) -> CompleteTurnResponse:
        """记录澄清文本并完成轮次。"""
        content = str(_args[-1])
        del semantic_fingerprint
        self.completed.append(content)
        return CompleteTurnResponse(message=_message(MessageRole.ASSISTANT, content))

    async def abandon_turn(self, *_args: object) -> None:
        """记录流结束后的执行权释放。"""
        self.abandoned += 1

    async def renew_turn(self, *_args: object) -> bool:
        """测试长轮次仍持有执行权。"""
        return True


class _ReplayConversations(_Conversations):
    """返回已经原子完成的同一轮次助手消息。"""

    async def assistant_message(self, *_args: object) -> MessageRecord | None:
        """提供幂等回放结果。"""
        return _message(MessageRole.ASSISTANT, "查询完成，共返回 3 行。")


class _ClarificationReplayConversations(_Conversations):
    """返回已经持久化为澄清终态的助手消息。"""

    async def assistant_message(self, *_args: object) -> MessageRecord | None:
        return _message(MessageRole.ASSISTANT, "请明确指标").model_copy(
            update={"semantic_fingerprint": "query:clarification"}
        )


class _IntentParser:
    """返回带精确用户原文证据的查询意图。"""

    async def parse(
        self,
        _question: str,
        _context_messages: list[str],
        _evidence_messages: list[str],
    ) -> QueryIntent:
        """返回一个聚合指标意图。"""
        return QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["销售额"],
        )


class _RecordingIntentParser(_IntentParser):
    """记录意图端口实际收到的证据链。"""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.evidence_messages: list[str] = []

    async def parse(
        self,
        question: str,
        context_messages: list[str],
        evidence_messages: list[str],
    ) -> QueryIntent:
        self.messages = context_messages
        self.evidence_messages = evidence_messages
        return await super().parse(question, context_messages, evidence_messages)


class _MultiClarificationIntentParser(_RecordingIntentParser):
    """返回能够由完整多轮用户证据证明的意图。"""

    async def parse(
        self,
        question: str,
        context_messages: list[str],
        evidence_messages: list[str],
    ) -> QueryIntent:
        self.messages = context_messages
        self.evidence_messages = evidence_messages
        return QueryIntent(
            query_type=QueryType.COMPARISON,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["销售额"],
            dimension_quotes=["地区"],
        )


class _NaturalTimeConversations(_Conversations):
    """Return the original time question, clarification, and short answer."""

    def __init__(self) -> None:
        super().__init__()
        self.chain = [
            _message(MessageRole.USER, "查询今年销售额总和"),
            _message(MessageRole.ASSISTANT, "请明确使用哪个时间字段").model_copy(
                update={"semantic_fingerprint": "query:clarification"}
            ),
            _message(MessageRole.USER, "下单时间"),
        ]


class _NaturalTimeIntentParser:
    """Reconstruct a trusted natural range from user-only chain evidence."""

    async def parse(
        self,
        question: str,
        context_messages: list[str],
        evidence_messages: list[str],
    ) -> QueryIntent:
        assert question == "下单时间"
        assert context_messages[0] == "user: 查询今年销售额总和"
        assert evidence_messages == ["查询今年销售额总和", "下单时间"]
        return QueryIntent(
            query_type=QueryType.AGGREGATE,
            aggregation="sum",
            aggregation_quote="总和",
            measure_quotes=["销售额"],
            time_quote="今年",
            time_column_quote="下单时间",
        )


class _NaturalTimeMetadata:
    """Bind both the measure and clarified temporal physical column."""

    async def build_context(
        self, question: str, intent: QueryIntent, schema: PhysicalSchema
    ) -> QueryContext:
        del question, intent
        table = schema.tables[0]
        columns = {column.name: column.id for column in table.columns}
        return QueryContext(
            physical_schema=schema,
            bindings={
                "销售额": columns["amount"],
                "下单时间": columns["created_at"],
            },
        )

    async def relationships_are_authoritative(self, schema: PhysicalSchema) -> bool:
        del schema
        return True

    async def bindings_are_authoritative(self, context: QueryContext) -> bool:
        del context
        return True


class _NaturalTimePlanner:
    """Emit only the two server-owned trusted temporal parameters."""

    async def draft(
        self,
        context: QueryContext,
        intent: QueryIntent,
        trusted_time_range: object,
    ) -> QueryDraft:
        del intent
        assert trusted_time_range is not None
        trusted = cast(object, trusted_time_range)
        table = context.physical_schema.tables[0]
        columns = {column.name: column.id for column in table.columns}
        return QueryDraft(
            sql=(
                "SELECT SUM(o.amount) AS total FROM dw.orders AS o "
                "WHERE o.created_at >= :trusted_time_start "
                "AND o.created_at < :trusted_time_end"
            ),
            params={
                "trusted_time_start": getattr(trusted, "start"),
                "trusted_time_end": getattr(trusted, "end"),
            },
            table_ids=[table.id],
            column_ids=[columns["amount"], columns["created_at"]],
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
            f"trusted range draft must validate: {context} {intent} "
            f"{trusted_time_range} {draft} {issues}"
        )


class _IndependentQueryConversations(_Conversations):
    """返回一个已完成旧查询和当前独立请求。"""

    async def start_turn(
        self, *_args: object, semantic_fingerprint: str | None = None
    ) -> StartTurnResponse:
        del semantic_fingerprint
        self.chain = [_message(MessageRole.USER, "查询销售额总和")]
        return StartTurnResponse(
            message=_message(MessageRole.USER, "查询销售额总和"),
            context=ConversationContext(
                messages=[
                    ContextMessage(role=MessageRole.USER, content="按月查看销售额趋势"),
                    ContextMessage(role=MessageRole.ASSISTANT, content="旧查询已完成"),
                    ContextMessage(role=MessageRole.USER, content="查询销售额总和"),
                ],
                memories=[],
            ),
            claim_token="c" * 32,
        )


class _ResolvedClarificationConversations(_Conversations):
    """返回已经由后续 Query 终态关闭的历史澄清。"""

    async def start_turn(
        self, *_args: object, semantic_fingerprint: str | None = None
    ) -> StartTurnResponse:
        del semantic_fingerprint
        self.chain = [_message(MessageRole.USER, "查询销售额总和")]
        return StartTurnResponse(
            message=_message(MessageRole.USER, "查询销售额总和"),
            context=ConversationContext(
                messages=[
                    ContextMessage(role=MessageRole.USER, content="按月查看销售额趋势"),
                    ContextMessage(
                        role=MessageRole.ASSISTANT,
                        content="请明确日期字段",
                        semantic_fingerprint="query:clarification",
                    ),
                    ContextMessage(role=MessageRole.USER, content="下单日期"),
                    ContextMessage(role=MessageRole.ASSISTANT, content="旧查询已完成"),
                    ContextMessage(role=MessageRole.USER, content="查询销售额总和"),
                ],
                memories=[],
            ),
            claim_token="c" * 32,
        )


class _MultiClarificationConversations(_Conversations):
    """返回同一查询连续两轮澄清后的完整消息链。"""

    async def start_turn(
        self, *_args: object, semantic_fingerprint: str | None = None
    ) -> StartTurnResponse:
        del semantic_fingerprint
        self.chain = [
            _message(MessageRole.USER, "按地区查询销售额总和"),
            _message(
                MessageRole.ASSISTANT, "销售额是下单金额还是支付金额？"
            ).model_copy(update={"semantic_fingerprint": "query:clarification"}),
            _message(MessageRole.USER, "支付金额"),
            _message(MessageRole.ASSISTANT, "要查询哪个地区？").model_copy(
                update={"semantic_fingerprint": "query:clarification"}
            ),
            _message(MessageRole.USER, "华东"),
        ]
        return StartTurnResponse(
            message=_message(MessageRole.USER, "华东"),
            context=ConversationContext(
                messages=[
                    ContextMessage(
                        role=MessageRole.USER, content="按地区查询销售额总和"
                    ),
                    ContextMessage(
                        role=MessageRole.ASSISTANT,
                        content="销售额是下单金额还是支付金额？",
                        semantic_fingerprint="query:clarification",
                    ),
                    ContextMessage(role=MessageRole.USER, content="支付金额"),
                    ContextMessage(
                        role=MessageRole.ASSISTANT,
                        content="要查询哪个地区？",
                        semantic_fingerprint="query:clarification",
                    ),
                    ContextMessage(role=MessageRole.USER, content="华东"),
                ],
                memories=[],
            ),
            claim_token="c" * 32,
        )


class _LongClarificationConversations(_Conversations):
    """返回超过普通 Conversation 窗口但仍在 Query 独立预算内的证据链。"""

    async def start_turn(
        self, *_args: object, semantic_fingerprint: str | None = None
    ) -> StartTurnResponse:
        del semantic_fingerprint
        filler = "补充口径" * 500
        self.chain = [
            *[
                _message(MessageRole.USER, f"{index}:{filler}")
                for index in range(21)
            ],
            _message(MessageRole.USER, "查询销售额总和"),
        ]
        return StartTurnResponse(
            message=self.chain[-1],
            context=ConversationContext(
                messages=[
                    ContextMessage(role=MessageRole.USER, content="查询销售额总和")
                ],
                memories=[],
            ),
            claim_token="c" * 32,
        )


class _OversizedClarificationConversations(_Conversations):
    """模拟持久化层对独立 Query 证据预算的稳定拒绝。"""

    async def pending_query_chain(self, *_args: object, **_kwargs: object):
        raise DataAgentError(
            "query_clarification_chain_too_large",
            "query_clarification_chain",
            "查询澄清证据链超过安全预算",
            http_status=422,
        )


class _Metadata:
    """模拟权威 Meta 对象仍有两个候选。"""

    async def build_context(self, *_args: object) -> QueryClarification:
        """返回本轮唯一的最高影响澄清问题。"""
        return QueryClarification(
            slot="measure",
            quote="销售额",
            question="“销售额”是下单金额还是支付金额？",
        )


class _MustNotRun:
    """若澄清分支越过门禁则立即使测试失败。"""

    def __getattr__(self, name: str) -> object:
        """拒绝任何未预期的后续调用。"""
        raise AssertionError(f"澄清分支不应调用 {name}")


async def test_planner_receives_configured_dw_database() -> None:
    """非默认 DW 数据库名必须进入生成提示负载。"""
    model = Mock(spec=BaseChatModel)
    runnable = AsyncMock()
    runnable.ainvoke.return_value = QueryDraft(
        sql="SELECT COUNT(*) FROM analytics.orders",
        table_ids=["table-orders"],
    )
    model.with_structured_output.return_value = runnable
    adapter = QueryLLMAdapter(cast(BaseChatModel, model), dw_database="analytics")

    await adapter.draft(
        QueryContext(
            physical_schema=PhysicalSchema(
                source="erp",
                canonical_ddl="",
                ddl_hash="ddl",
                schema_fingerprint="schema",
                tables=[],
            )
        ),
        QueryIntent(query_type=QueryType.AGGREGATE),
        None,
    )

    messages = runnable.ainvoke.await_args.args[0]
    payload = json.loads(messages[1][1])
    assert payload["dw_database"] == "analytics"


@pytest.mark.parametrize("method", ["draft", "repair"])
async def test_planner_maps_malformed_structured_draft(method: str) -> None:
    """初稿和修复稿的结构化契约异常都收敛为稳定 Query 错误。"""
    model = Mock(spec=BaseChatModel)
    runnable = AsyncMock()
    runnable.ainvoke.return_value = object()
    model.with_structured_output.return_value = runnable
    adapter = QueryLLMAdapter(cast(BaseChatModel, model), dw_database="dw")
    context = QueryContext(
        physical_schema=PhysicalSchema(
            source="erp",
            canonical_ddl="",
            ddl_hash="ddl",
            schema_fingerprint="schema",
            tables=[],
        )
    )
    intent = QueryIntent(query_type=QueryType.DETAIL)

    with pytest.raises(DataAgentError, match="SQL 草稿|修复草稿") as captured:
        if method == "draft":
            await adapter.draft(context, intent, None)
        else:
            await adapter.repair(
                context,
                intent,
                None,
                QueryDraft(sql="SELECT 1", table_ids=["table"]),
                (SQLValidationIssue(code="invalid"),),
            )
    assert captured.value.code == "query_model_invalid"


async def test_stream_completes_only_one_authoritative_clarification() -> None:
    """权威绑定歧义只完成一个澄清轮次且不生成或执行 SQL。"""
    conversations = _Conversations()
    application = QueryApplication(
        conversations=cast(ConversationPort, conversations),
        intents=cast(QueryIntentPort, _IntentParser()),
        metadata=cast(QueryMetadataPort, _Metadata()),
        planner=cast(QueryPlannerPort, _MustNotRun()),
        readiness=cast(QueryReadinessPort, _MustNotRun()),
        executor=cast(QueryExecutorPort, _MustNotRun()),
        dw_database="dw",
    )
    request = QueryRequest(
        user_id="user-1",
        conversation_uid="conversation-1",
        turn_uid="turn-1",
        question="查询销售额",
        supplemental_context=_SUPPLEMENTAL_CONTEXT,
        ddl_context=DDLJobRequest(
            source="erp",
            ddl="CREATE TABLE orders (id BIGINT PRIMARY KEY, amount DECIMAL(10,2))",
        ),
    )

    events = [event async for event in application.stream(request)]

    assert [event.kind for event in events] == ["clarification"]
    assert events[0].message == "“销售额”是下单金额还是支付金额？"
    assert conversations.completed == ["“销售额”是下单金额还是支付金额？"]


async def test_independent_query_does_not_reuse_completed_history_as_evidence() -> None:
    """新独立问题不能被旧趋势轮次的显式槽位污染。"""
    parser = _RecordingIntentParser()
    application = QueryApplication(
        conversations=cast(ConversationPort, _IndependentQueryConversations()),
        intents=cast(QueryIntentPort, parser),
        metadata=cast(QueryMetadataPort, _Metadata()),
        planner=cast(QueryPlannerPort, _MustNotRun()),
        readiness=cast(QueryReadinessPort, _MustNotRun()),
        executor=cast(QueryExecutorPort, _MustNotRun()),
        dw_database="dw",
    )
    request = QueryRequest(
        user_id="user-1",
        conversation_uid="conversation-1",
        turn_uid="turn-1",
        question="查询销售额总和",
        supplemental_context=_SUPPLEMENTAL_CONTEXT,
        ddl_context=DDLJobRequest(
            source="erp",
            ddl="CREATE TABLE orders (id BIGINT PRIMARY KEY, amount DECIMAL(10,2))",
        ),
    )

    await anext(application.stream(request))

    assert parser.messages == ["user: 查询销售额总和"]


async def test_resolved_clarification_does_not_pollute_independent_query() -> None:
    """澄清后的普通终态会关闭证据链。"""
    parser = _RecordingIntentParser()
    application = QueryApplication(
        conversations=cast(ConversationPort, _ResolvedClarificationConversations()),
        intents=cast(QueryIntentPort, parser),
        metadata=cast(QueryMetadataPort, _Metadata()),
        planner=cast(QueryPlannerPort, _MustNotRun()),
        readiness=cast(QueryReadinessPort, _MustNotRun()),
        executor=cast(QueryExecutorPort, _MustNotRun()),
        dw_database="dw",
    )
    await anext(
        application.stream(
            QueryRequest(
                user_id="user-1",
                conversation_uid="conversation-1",
                turn_uid="turn-2",
                question="查询销售额总和",
                supplemental_context=_SUPPLEMENTAL_CONTEXT,
                ddl_context=DDLJobRequest(
                    source="erp",
                    ddl=(
                        "CREATE TABLE orders "
                        "(id BIGINT PRIMARY KEY, amount DECIMAL(10,2))"
                    ),
                ),
            )
        )
    )
    assert parser.messages == ["user: 查询销售额总和"]


async def test_multi_round_clarification_keeps_original_user_evidence() -> None:
    """连续澄清必须保留原始问题和所有用户回答，助手文本不能充当证据。"""
    parser = _MultiClarificationIntentParser()
    application = QueryApplication(
        conversations=cast(ConversationPort, _MultiClarificationConversations()),
        intents=cast(QueryIntentPort, parser),
        metadata=cast(QueryMetadataPort, _Metadata()),
        planner=cast(QueryPlannerPort, _MustNotRun()),
        readiness=cast(QueryReadinessPort, _MustNotRun()),
        executor=cast(QueryExecutorPort, _MustNotRun()),
        dw_database="dw",
    )
    await anext(
        application.stream(
            QueryRequest(
                user_id="user-1",
                conversation_uid="conversation-1",
                turn_uid="turn-3",
                question="华东",
                supplemental_context=_SUPPLEMENTAL_CONTEXT,
                ddl_context=DDLJobRequest(
                    source="erp",
                    ddl="CREATE TABLE orders (id BIGINT PRIMARY KEY)",
                ),
            )
        )
    )
    assert parser.messages[0] == "user: 按地区查询销售额总和"
    assert parser.evidence_messages == ["按地区查询销售额总和", "支付金额", "华东"]


async def test_time_column_answer_executes_the_original_natural_range() -> None:
    """A short time-column answer completes the original trusted range flow."""
    executor = _Executor()
    application = QueryApplication(
        conversations=cast(ConversationPort, _NaturalTimeConversations()),
        intents=cast(QueryIntentPort, _NaturalTimeIntentParser()),
        metadata=cast(QueryMetadataPort, _NaturalTimeMetadata()),
        planner=cast(QueryPlannerPort, _NaturalTimePlanner()),
        readiness=cast(QueryReadinessPort, _Ready()),
        executor=cast(QueryExecutorPort, executor),
        dw_database="dw",
        now=lambda: datetime(2026, 8, 8, tzinfo=UTC),
    )
    request = QueryRequest(
        user_id="user-1",
        conversation_uid="conversation-1",
        turn_uid="turn-time-column",
        question="下单时间",
        supplemental_context=_SUPPLEMENTAL_CONTEXT,
        ddl_context=DDLJobRequest(
            source="erp",
            ddl=(
                "CREATE TABLE orders (id BIGINT PRIMARY KEY, "
                "amount DECIMAL(10,2), created_at TIMESTAMP)"
            ),
        ),
    )

    events = [event async for event in application.stream(request)]

    assert events[-1].kind == "complete"
    assert executor.explained == 2
    assert executor.executed == 1


async def test_query_uses_durable_chain_beyond_ordinary_context_budgets() -> None:
    """Query 意图证据不得被 20 条/32768 字符的普通上下文预算截断。"""
    parser = _RecordingIntentParser()
    application = QueryApplication(
        conversations=cast(ConversationPort, _LongClarificationConversations()),
        intents=cast(QueryIntentPort, parser),
        metadata=cast(QueryMetadataPort, _Metadata()),
        planner=cast(QueryPlannerPort, _MustNotRun()),
        readiness=cast(QueryReadinessPort, _MustNotRun()),
        executor=cast(QueryExecutorPort, _MustNotRun()),
        dw_database="dw",
    )
    request = QueryRequest(
        user_id="user-1",
        conversation_uid="conversation-1",
        turn_uid="turn-long",
        question="查询销售额总和",
        supplemental_context=_SUPPLEMENTAL_CONTEXT,
        ddl_context=DDLJobRequest(
            source="erp",
            ddl="CREATE TABLE orders (id BIGINT PRIMARY KEY, amount DECIMAL(10,2))",
        ),
    )

    await anext(application.stream(request))

    assert len(parser.messages) == 22
    assert sum(map(len, parser.messages)) > 32_768
    assert parser.evidence_messages[-1] == "查询销售额总和"


async def test_query_propagates_independent_clarification_budget_overflow() -> None:
    """独立澄清链超预算必须稳定失败并释放当前 turn claim。"""
    conversations = _OversizedClarificationConversations()
    application = QueryApplication(
        conversations=cast(ConversationPort, conversations),
        intents=cast(QueryIntentPort, _MustNotRun()),
        metadata=cast(QueryMetadataPort, _MustNotRun()),
        planner=cast(QueryPlannerPort, _MustNotRun()),
        readiness=cast(QueryReadinessPort, _MustNotRun()),
        executor=cast(QueryExecutorPort, _MustNotRun()),
        dw_database="dw",
    )
    request = QueryRequest(
        user_id="user-1",
        conversation_uid="conversation-1",
        turn_uid="turn-large",
        question="查询销售额总和",
        supplemental_context=_SUPPLEMENTAL_CONTEXT,
        ddl_context=DDLJobRequest(
            source="erp",
            ddl="CREATE TABLE orders (id BIGINT PRIMARY KEY)",
        ),
    )

    with pytest.raises(DataAgentError) as captured:
        _ = [event async for event in application.stream(request)]

    assert captured.value.code == "query_clarification_chain_too_large"
    assert conversations.abandoned == 1


async def test_completed_turn_replays_without_duplicate_query_work() -> None:
    """同一 turn_uid 回放已完成文本且不重复意图、召回或执行。"""
    application = QueryApplication(
        conversations=cast(ConversationPort, _ReplayConversations()),
        intents=cast(QueryIntentPort, _MustNotRun()),
        metadata=cast(QueryMetadataPort, _MustNotRun()),
        planner=cast(QueryPlannerPort, _MustNotRun()),
        readiness=cast(QueryReadinessPort, _MustNotRun()),
        executor=cast(QueryExecutorPort, _MustNotRun()),
        dw_database="dw",
    )
    request = QueryRequest(
        user_id="user-1",
        conversation_uid="conversation-1",
        turn_uid="turn-1",
        question="查询销售额",
        supplemental_context=_SUPPLEMENTAL_CONTEXT,
        ddl_context=DDLJobRequest(
            source="erp",
            ddl="CREATE TABLE orders (id BIGINT PRIMARY KEY, amount DECIMAL(10,2))",
        ),
    )

    events = [event async for event in application.stream(request)]

    assert [event.kind for event in events] == ["complete"]
    assert events[0].message == "查询完成，共返回 3 行。"


async def test_clarification_replay_preserves_terminal_event_kind() -> None:
    """同轮次澄清回放保持 clarification，不能伪装为查询完成。"""
    application = QueryApplication(
        conversations=cast(ConversationPort, _ClarificationReplayConversations()),
        intents=cast(QueryIntentPort, _MustNotRun()),
        metadata=cast(QueryMetadataPort, _MustNotRun()),
        planner=cast(QueryPlannerPort, _MustNotRun()),
        readiness=cast(QueryReadinessPort, _MustNotRun()),
        executor=cast(QueryExecutorPort, _MustNotRun()),
        dw_database="dw",
    )
    events = [
        event
        async for event in application.stream(
            QueryRequest(
                user_id="user-1",
                conversation_uid="conversation-1",
                turn_uid="turn-1",
                question="查询销售额",
                supplemental_context=_SUPPLEMENTAL_CONTEXT,
                ddl_context=DDLJobRequest(
                    source="erp", ddl="CREATE TABLE orders (id BIGINT PRIMARY KEY)"
                ),
            )
        )
    ]
    assert [event.kind for event in events] == ["clarification"]


class _GroundedMetadata:
    """返回已经唯一绑定的当前 DDL 上下文。"""

    async def build_context(
        self,
        question: str,
        intent: QueryIntent,
        schema: PhysicalSchema,
    ) -> QueryContext:
        """使用编排传入的确定性物理模式。"""
        del question, intent
        return QueryContext(physical_schema=schema)

    async def relationships_are_authoritative(self, schema: PhysicalSchema) -> bool:
        """测试上下文始终对应当前权威快照。"""
        del schema
        return True

    async def bindings_are_authoritative(self, context: QueryContext) -> bool:
        """测试上下文的语义绑定始终保持稳定。"""
        del context
        return True


class _Planner:
    """返回一条不带业务总量 LIMIT 的权威对象草稿。"""

    repairs = 0

    async def draft(
        self, context: QueryContext, intent: QueryIntent, trusted_time_range: object
    ) -> QueryDraft:
        """生成初始 SQL 草稿。"""
        del intent, trusted_time_range
        table = context.physical_schema.tables[0]
        return QueryDraft(
            sql="SELECT SUM(o.amount) AS total FROM dw.orders AS o",
            table_ids=[table.id],
            column_ids=[
                next(column.id for column in table.columns if column.name == "amount")
            ],
        )

    async def repair(
        self,
        context: QueryContext,
        intent: QueryIntent,
        trusted_time_range: object,
        draft: QueryDraft,
        issues: tuple[SQLValidationIssue, ...],
    ) -> QueryDraft:
        """记录不应发生的修复。"""
        del draft, issues
        self.repairs += 1
        return await self.draft(context, intent, trusted_time_range)


class _Ready:
    """表示 AST 解析的全部目标表已就绪。"""

    async def ready(self, target_tables: tuple[str, ...]) -> bool:
        """允许自动执行。"""
        del target_tables
        return True

    @asynccontextmanager
    async def hold(self, target_tables: tuple[str, ...]):
        """模拟执行期间持有同步代次。"""
        del target_tables
        yield


class _Executor:
    """从专用只读 seam 返回两个批次。"""

    explained = 0
    executed = 0

    async def explain(self, query: ValidatedQuery) -> None:
        """记录数据库预检。"""
        del query
        self.explained += 1

    async def execute(self, query: ValidatedQuery):
        """返回完整结果的两个后续批次。"""
        del query
        self.executed += 1
        yield QueryBatch(columns=["total"], rows=[[1], [2]])
        yield QueryBatch(columns=["total"], rows=[[3]])


class _CoordinatedReadiness(_Ready):
    """公开当前 generation READ 临界区，供跨层行为断言。"""

    def __init__(self) -> None:
        self.depth = 0

    async def ready(self, target_tables: tuple[str, ...]) -> bool:
        assert self.depth == 1
        return await super().ready(target_tables)

    @asynccontextmanager
    async def hold(self, target_tables: tuple[str, ...]):
        del target_tables
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1


class _CoordinatedMetadata(_GroundedMetadata):
    """断言 authority recheck 与 EXPLAIN 共用同一 READ 临界区。"""

    def __init__(self, readiness: _CoordinatedReadiness) -> None:
        self._readiness = readiness

    async def relationships_are_authoritative(self, schema: PhysicalSchema) -> bool:
        assert self._readiness.depth == 1
        return await super().relationships_are_authoritative(schema)

    async def bindings_are_authoritative(self, context: QueryContext) -> bool:
        assert self._readiness.depth == 1
        return await super().bindings_are_authoritative(context)


class _CoordinatedExecutor(_Executor):
    """断言规划 EXPLAIN、最终 EXPLAIN 和流式 SELECT 均受 READ 保护。"""

    def __init__(self, readiness: _CoordinatedReadiness) -> None:
        self._readiness = readiness
        self.explained = 0
        self.executed = 0

    async def explain(self, query: ValidatedQuery) -> None:
        assert self._readiness.depth == 1
        await super().explain(query)

    async def execute(self, query: ValidatedQuery):
        assert self._readiness.depth == 1
        async for batch in super().execute(query):
            assert self._readiness.depth == 1
            yield batch


class _ChangingTargetPlanner(_Planner):
    """首稿与修复稿分别引用不同的权威目标表。"""

    @staticmethod
    def _draft_for(context: QueryContext, table_name: str) -> QueryDraft:
        table = next(
            table
            for table in context.physical_schema.tables
            if table.name == table_name
        )
        amount = next(column for column in table.columns if column.name == "amount")
        return QueryDraft(
            sql=f"SELECT SUM(t.amount) AS total FROM dw.{table_name} AS t",
            table_ids=[table.id],
            column_ids=[amount.id],
        )

    async def draft(
        self, context: QueryContext, intent: QueryIntent, trusted_time_range: object
    ) -> QueryDraft:
        """让首次数据库预检使用 orders。"""
        del intent, trusted_time_range
        return self._draft_for(context, "orders")

    async def repair(
        self,
        context: QueryContext,
        intent: QueryIntent,
        trusted_time_range: object,
        draft: QueryDraft,
        issues: tuple[SQLValidationIssue, ...],
    ) -> QueryDraft:
        """把被 EXPLAIN 拒绝的首稿改为 refunds 目标。"""
        del intent, trusted_time_range, draft
        assert issues == (SQLValidationIssue(code="explain_rejected"),)
        return self._draft_for(context, "refunds")


class _TargetRecordingReadiness(_Ready):
    """记录每一次 generation READ set 的实际目标。"""

    def __init__(self) -> None:
        self.held: list[tuple[str, ...]] = []

    @asynccontextmanager
    async def hold(self, target_tables: tuple[str, ...]):
        self.held.append(target_tables)
        yield


class _RejectFirstExplainExecutor(_Executor):
    """只拒绝首稿，使应用消费一次受协调的 repair 预算。"""

    def __init__(self) -> None:
        self.explained = 0
        self.executed = 0

    async def explain(self, query: ValidatedQuery) -> None:
        self.explained += 1
        if self.explained == 1:
            raise QueryExplainRejected(SQLValidationIssue(code="explain_rejected"))
        assert query.target_tables == ("refunds",)


class _EmptyExecutor(_Executor):
    """返回带字段名的空结果。"""

    async def execute(self, query: ValidatedQuery):
        """保留数据库返回的空结果元数据。"""
        del query
        self.executed += 1
        yield QueryBatch(columns=["total"], rows=[])


class _ClosableExecutor(_Executor):
    """记录消费方提前关闭时底层流清理。"""

    closed = False

    async def execute(self, query: ValidatedQuery):
        """在生成器关闭路径记录资源释放。"""
        del query
        try:
            yield QueryBatch(columns=["total"], rows=[[1]])
            yield QueryBatch(columns=["total"], rows=[[2]])
        finally:
            self.closed = True


class _ReleaseFailingReadiness(_Ready):
    """模拟业务异常清理期间 generation owner 连接释放失败。"""

    def __init__(self) -> None:
        self.holds = 0

    @asynccontextmanager
    async def hold(self, target_tables: tuple[str, ...]):
        """进入临界区后在退出时抛出锁清理错误。"""
        del target_tables
        self.holds += 1
        yield
        if self.holds > 1:
            raise AdvisoryLockReleaseError("release failed")


class _ContendedReadiness(_Ready):
    """模拟 generation guard 在进入阶段竞争失败。"""

    @asynccontextmanager
    async def hold(self, target_tables: tuple[str, ...]):
        del target_tables
        raise DataAgentError(
            "generation_lock_unavailable",
            "query_readiness",
            "generation 正在变更",
            retryable=True,
            http_status=409,
        )
        yield


class _FailingExecuteExecutor(_Executor):
    """在 generation 临界区内抛出原始查询异常。"""

    async def execute(self, query: ValidatedQuery):
        """在首批结果前失败。"""
        del query
        raise ValueError("query failed")
        yield


async def test_stream_explains_checks_readiness_and_keeps_all_batches() -> None:
    """门禁通过后自动执行并把全部只读批次流式返回。"""
    conversations = _Conversations()
    planner = _Planner()
    executor = _Executor()
    application = QueryApplication(
        conversations=cast(ConversationPort, conversations),
        intents=cast(QueryIntentPort, _IntentParser()),
        metadata=cast(QueryMetadataPort, _GroundedMetadata()),
        planner=cast(QueryPlannerPort, planner),
        readiness=cast(QueryReadinessPort, _Ready()),
        executor=cast(QueryExecutorPort, executor),
        dw_database="dw",
    )
    request = QueryRequest(
        user_id="user-1",
        conversation_uid="conversation-1",
        turn_uid="turn-1",
        question="查询销售额",
        supplemental_context=_SUPPLEMENTAL_CONTEXT,
        ddl_context=DDLJobRequest(
            source="erp",
            ddl=("CREATE TABLE orders (id BIGINT PRIMARY KEY, amount DECIMAL(10,2))"),
        ),
    )

    events = [event async for event in application.stream(request)]

    assert [event.kind for event in events] == ["metadata", "rows", "rows", "complete"]
    assert [row for event in events for row in event.rows] == [[1], [2], [3]]
    assert events[-1].row_count == 3
    # 规划预检后，在 generation lock 内再次预检以消除就绪检查竞态。
    assert executor.explained == 2
    assert executor.executed == 1
    assert planner.repairs == 0


async def test_guard_entry_failure_preserves_error_and_abandons_turn() -> None:
    """锁竞争错误不得被未进入 guard 的清理覆盖，轮次必须立即释放。"""
    conversations = _Conversations()
    application = QueryApplication(
        conversations=cast(ConversationPort, conversations),
        intents=cast(QueryIntentPort, _IntentParser()),
        metadata=cast(QueryMetadataPort, _GroundedMetadata()),
        planner=cast(QueryPlannerPort, _Planner()),
        readiness=cast(QueryReadinessPort, _ContendedReadiness()),
        executor=cast(QueryExecutorPort, _Executor()),
        dw_database="dw",
    )
    request = QueryRequest(
        user_id="user-1",
        conversation_uid="conversation-1",
        turn_uid="turn-1",
        question="查询销售额",
        supplemental_context=_SUPPLEMENTAL_CONTEXT,
        ddl_context=DDLJobRequest(
            source="erp",
            ddl="CREATE TABLE orders (id BIGINT PRIMARY KEY, amount DECIMAL(10,2))",
        ),
    )

    with pytest.raises(DataAgentError) as captured:
        _ = [event async for event in application.stream(request)]

    assert captured.value.code == "generation_lock_unavailable"
    assert conversations.abandoned == 1


@pytest.mark.parametrize("executor_type", [_EmptyExecutor, _ClosableExecutor])
async def test_stream_preserves_empty_metadata_and_closes_early_stream(
    executor_type: type[_Executor],
) -> None:
    """空结果保留字段名，消费方提前停止时确定性关闭 executor 流。"""
    executor = executor_type()
    application = QueryApplication(
        conversations=cast(ConversationPort, _Conversations()),
        intents=cast(QueryIntentPort, _IntentParser()),
        metadata=cast(QueryMetadataPort, _GroundedMetadata()),
        planner=cast(QueryPlannerPort, _Planner()),
        readiness=cast(QueryReadinessPort, _Ready()),
        executor=cast(QueryExecutorPort, executor),
        dw_database="dw",
    )
    request = QueryRequest(
        user_id="user-1",
        conversation_uid="conversation-1",
        turn_uid="turn-1",
        question="查询销售额",
        supplemental_context=_SUPPLEMENTAL_CONTEXT,
        ddl_context=DDLJobRequest(
            source="erp",
            ddl="CREATE TABLE orders (id BIGINT PRIMARY KEY, amount DECIMAL(10,2))",
        ),
    )
    stream = application.stream(request)

    metadata = await anext(stream)
    await stream.aclose()

    assert metadata.kind == "metadata"
    assert metadata.columns == ["total"]
    if isinstance(executor, _ClosableExecutor):
        assert executor.closed is True


async def test_generation_cleanup_failure_does_not_replace_query_error() -> None:
    """Generation 清理失败不得覆盖临界区内的原始查询异常。"""
    application = QueryApplication(
        conversations=cast(ConversationPort, _Conversations()),
        intents=cast(QueryIntentPort, _IntentParser()),
        metadata=cast(QueryMetadataPort, _GroundedMetadata()),
        planner=cast(QueryPlannerPort, _Planner()),
        readiness=cast(QueryReadinessPort, _ReleaseFailingReadiness()),
        executor=cast(QueryExecutorPort, _FailingExecuteExecutor()),
        dw_database="dw",
    )
    request = QueryRequest(
        user_id="user-1",
        conversation_uid="conversation-1",
        turn_uid="turn-1",
        question="查询销售额",
        supplemental_context=_SUPPLEMENTAL_CONTEXT,
        ddl_context=DDLJobRequest(
            source="erp",
            ddl="CREATE TABLE orders (id BIGINT PRIMARY KEY, amount DECIMAL(10,2))",
        ),
    )

    with pytest.raises(ValueError, match="query failed"):
        _ = [event async for event in application.stream(request)]


class _RepairPlanner(_Planner):
    """首稿违反星号规则，唯一修复返回安全草稿。"""

    async def draft(
        self, context: QueryContext, intent: QueryIntent, trusted_time_range: object
    ) -> QueryDraft:
        """返回需要静态修复的首稿。"""
        safe = await super().draft(context, intent, trusted_time_range)
        return safe.model_copy(update={"sql": "SELECT * FROM dw.orders"})

    async def repair(
        self,
        context: QueryContext,
        intent: QueryIntent,
        trusted_time_range: object,
        draft: QueryDraft,
        issues: tuple[SQLValidationIssue, ...],
    ) -> QueryDraft:
        """消费一次稳定问题反馈并返回安全草稿。"""
        del draft
        assert issues[0].code == "select_star"
        self.repairs += 1
        return await _Planner.draft(self, context, intent, trusted_time_range)


class _TimeoutExecutor(_Executor):
    """模拟 EXPLAIN 基础设施超时。"""

    async def explain(self, query: ValidatedQuery) -> None:
        """抛出不允许模型修复的稳定超时。"""
        del query
        raise DataAgentError(
            "query_timeout",
            "query_explain",
            "预检超时",
            http_status=504,
        )


class _FinalExplainFailingExecutor(_Executor):
    """模拟规划预检通过后最终 EXPLAIN 失败。"""

    async def explain(self, query: ValidatedQuery) -> None:
        """第二次 EXPLAIN 抛出稳定数据库错误。"""
        await super().explain(query)
        if self.explained == 2:
            raise DataAgentError(
                "query_timeout",
                "query_explain",
                "最终预检超时",
                http_status=504,
        )


async def test_authority_readiness_explain_and_select_share_generation_read() -> None:
    """每个决定性数据库门禁都必须位于相同 target READ 临界区。"""
    readiness = _CoordinatedReadiness()
    executor = _CoordinatedExecutor(readiness)
    application = QueryApplication(
        conversations=cast(ConversationPort, _Conversations()),
        intents=cast(QueryIntentPort, _IntentParser()),
        metadata=cast(QueryMetadataPort, _CoordinatedMetadata(readiness)),
        planner=cast(QueryPlannerPort, _Planner()),
        readiness=cast(QueryReadinessPort, readiness),
        executor=cast(QueryExecutorPort, executor),
        dw_database="dw",
    )
    request = QueryRequest(
        user_id="user-1",
        conversation_uid="conversation-1",
        turn_uid="turn-coordinated",
        question="查询销售额总和",
        supplemental_context=_SUPPLEMENTAL_CONTEXT,
        ddl_context=DDLJobRequest(
            source="erp",
            ddl="CREATE TABLE orders (id BIGINT PRIMARY KEY, amount DECIMAL(10,2))",
        ),
    )

    events = [event async for event in application.stream(request)]

    assert events[-1].kind == "complete"
    assert executor.explained == 2
    assert executor.executed == 1
    assert readiness.depth == 0


async def test_explain_repair_reacquires_the_repaired_target_read_set() -> None:
    """Repair 改变 AST 目标时不得沿用首稿的 generation READ set。"""
    readiness = _TargetRecordingReadiness()
    executor = _RejectFirstExplainExecutor()
    application = QueryApplication(
        conversations=cast(ConversationPort, _Conversations()),
        intents=cast(QueryIntentPort, _IntentParser()),
        metadata=cast(QueryMetadataPort, _GroundedMetadata()),
        planner=cast(QueryPlannerPort, _ChangingTargetPlanner()),
        readiness=cast(QueryReadinessPort, readiness),
        executor=cast(QueryExecutorPort, executor),
        dw_database="dw",
    )
    request = QueryRequest(
        user_id="user-1",
        conversation_uid="conversation-1",
        turn_uid="turn-repair-target",
        question="查询销售额总和",
        supplemental_context=_SUPPLEMENTAL_CONTEXT,
        ddl_context=DDLJobRequest(
            source="erp",
            ddl=(
                "CREATE TABLE orders (id BIGINT PRIMARY KEY, amount DECIMAL(10,2));"
                "CREATE TABLE refunds (id BIGINT PRIMARY KEY, amount DECIMAL(10,2))"
            ),
        ),
    )

    events = [event async for event in application.stream(request)]

    assert events[-1].kind == "complete"
    assert readiness.held == [("orders",), ("refunds",), ("refunds",)]
    assert executor.explained == 3


async def test_final_explain_has_execution_started_and_failed_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """最终 EXPLAIN 必须位于同一执行审计的开始与失败终态之间。"""
    audit_events: list[tuple[str, dict[str, object]]] = []

    def capture_audit(_level: str, _message: str, **fields: object) -> None:
        audit_events.append((_message, fields))

    monkeypatch.setattr("query.application.service.structured_log", capture_audit)
    application = QueryApplication(
        conversations=cast(ConversationPort, _Conversations()),
        intents=cast(QueryIntentPort, _IntentParser()),
        metadata=cast(QueryMetadataPort, _GroundedMetadata()),
        planner=cast(QueryPlannerPort, _Planner()),
        readiness=cast(QueryReadinessPort, _Ready()),
        executor=cast(QueryExecutorPort, _FinalExplainFailingExecutor()),
        dw_database="dw",
    )
    request = QueryRequest(
        user_id="user-1",
        conversation_uid="conversation-1",
        turn_uid="turn-1",
        question="查询销售额",
        supplemental_context=_SUPPLEMENTAL_CONTEXT,
        ddl_context=DDLJobRequest(
            source="erp",
            ddl="CREATE TABLE orders (id BIGINT PRIMARY KEY, amount DECIMAL(10,2))",
        ),
    )

    with pytest.raises(DataAgentError, match="最终预检超时"):
        _ = [event async for event in application.stream(request)]

    execution_outcomes = [
        fields["outcome"]
        for message, fields in audit_events
        if message.startswith("只读查询执行")
    ]
    assert execution_outcomes == ["started", "failed"]


async def test_stream_repairs_sql_once_but_never_repairs_timeout() -> None:
    """SQL 规则失败只修复一次，基础设施超时直接向调用方传播。"""
    request = QueryRequest(
        user_id="user-1",
        conversation_uid="conversation-1",
        turn_uid="turn-1",
        question="查询销售额",
        supplemental_context=_SUPPLEMENTAL_CONTEXT,
        ddl_context=DDLJobRequest(
            source="erp",
            ddl="CREATE TABLE orders (id BIGINT PRIMARY KEY, amount DECIMAL(10,2))",
        ),
    )
    planner = _RepairPlanner()
    application = QueryApplication(
        conversations=cast(ConversationPort, _Conversations()),
        intents=cast(QueryIntentPort, _IntentParser()),
        metadata=cast(QueryMetadataPort, _GroundedMetadata()),
        planner=cast(QueryPlannerPort, planner),
        readiness=cast(QueryReadinessPort, _Ready()),
        executor=cast(QueryExecutorPort, _Executor()),
        dw_database="dw",
    )

    events = [event async for event in application.stream(request)]

    assert events[-1].kind == "complete"
    assert planner.repairs == 1

    timeout_planner = _Planner()
    timeout_conversations = _Conversations()
    timeout_application = QueryApplication(
        conversations=cast(ConversationPort, timeout_conversations),
        intents=cast(QueryIntentPort, _IntentParser()),
        metadata=cast(QueryMetadataPort, _GroundedMetadata()),
        planner=cast(QueryPlannerPort, timeout_planner),
        readiness=cast(QueryReadinessPort, _Ready()),
        executor=cast(QueryExecutorPort, _TimeoutExecutor()),
        dw_database="dw",
    )
    with pytest.raises(DataAgentError, match="预检超时"):
        _ = [event async for event in timeout_application.stream(request)]
    assert timeout_planner.repairs == 0
    assert timeout_conversations.abandoned == 1
