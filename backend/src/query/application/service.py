"""自然语言只读查询的唯一应用编排入口。"""

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator
from time import perf_counter

from loguru import logger

from answer_readiness.service import DATA_PREPARING_MESSAGE
from app_logging import structured_log
from conversation.models import MessageRole
from ddl_metadata.parsing import parse_ddl
from errors import DataAgentError
from query.application.contracts import (
    ConversationPort,
    QueryClarification,
    QueryEvent,
    QueryExecutorPort,
    QueryExplainRejected,
    QueryIntentPort,
    QueryMetadataPort,
    QueryPlannerPort,
    QueryReadinessPort,
    QueryRequest,
)
from query.domain import (
    QueryContext,
    QueryIntent,
    ValidatedQuery,
    validate_query,
)


class QueryApplication:
    """隐藏意图、召回、校验、就绪和只读执行顺序的深模块。"""

    def __init__(
        self,
        *,
        conversations: ConversationPort,
        intents: QueryIntentPort,
        metadata: QueryMetadataPort,
        planner: QueryPlannerPort,
        readiness: QueryReadinessPort,
        executor: QueryExecutorPort,
        dw_database: str,
        turn_lease_seconds: int = 600,
    ) -> None:
        """绑定查询流程的外部端口。"""
        self._conversations = conversations
        self._intents = intents
        self._metadata = metadata
        self._planner = planner
        self._readiness = readiness
        self._executor = executor
        self._dw_database = dw_database
        self._turn_lease_seconds = turn_lease_seconds

    async def stream(self, request: QueryRequest) -> AsyncGenerator[QueryEvent, None]:
        """执行一轮查询并逐个产生有界 NDJSON 事件。"""
        # 步骤一：在占用 Conversation 门禁前确定性解析当前 DDL。
        schema = await parse_ddl(request.ddl_context.source, request.ddl_context.ddl)
        # 步骤二：提交用户消息并复用已有 Conversation 上下文和幂等坐标。
        started = await self._conversations.start_turn(
            request.user_id,
            request.conversation_uid,
            request.turn_uid,
            request.question,
            semantic_fingerprint=hashlib.sha256(
                json.dumps(
                    {
                        "entrypoint": "query",
                        "question": request.question,
                        "source": request.ddl_context.source,
                        "schema_fingerprint": schema.schema_fingerprint,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        )
        try:
            existing = await self._conversations.assistant_message(
                request.user_id,
                request.conversation_uid,
                request.turn_uid,
            )
        except BaseException:
            if started.execution_owner:
                await self._conversations.abandon_turn(
                    request.user_id, request.conversation_uid, request.turn_uid
                )
            raise
        if existing is not None:
            yield QueryEvent(
                kind=(
                    "clarification"
                    if existing.semantic_fingerprint == "query:clarification"
                    else "complete"
                ),
                message=existing.content,
            )
            return
        if not started.execution_owner:
            raise DataAgentError(
                "query_in_progress",
                "query_turn",
                "相同轮次正在执行",
                retryable=True,
                http_status=409,
            )
        owner_task = asyncio.current_task()
        heartbeat = asyncio.create_task(
            self._heartbeat_turn(request, owner_task),
            name=f"query-turn-heartbeat:{request.turn_uid}",
        )
        try:
            # 步骤三：QueryIntent 只消费同一有界上下文中的用户原文证据。
            messages = started.context.messages
            clarification_indexes = [
                index
                for index, message in enumerate(messages)
                if message.role == MessageRole.ASSISTANT
                and message.semantic_fingerprint == "query:clarification"
            ]
            pending_clarification = bool(clarification_indexes) and not any(
                message.role == MessageRole.ASSISTANT
                for message in messages[clarification_indexes[-1] + 1 :]
            )
            if pending_clarification:
                clarification_index = clarification_indexes[-1]
                prior_terminal_indexes = [
                    index
                    for index in range(clarification_index)
                    if messages[index].role == MessageRole.ASSISTANT
                    and messages[index].semantic_fingerprint
                    != "query:clarification"
                ]
                terminal_index = (
                    prior_terminal_indexes[-1] if prior_terminal_indexes else -1
                )
                prior_user_indexes = [
                    index
                    for index in range(terminal_index + 1, clarification_index)
                    if messages[index].role == MessageRole.USER
                ]
                chain_start = (
                    prior_user_indexes[0]
                    if prior_user_indexes
                    else clarification_index
                )
                evidence_chain = messages[chain_start:]
            else:
                current_user_indexes = [
                    index
                    for index, message in enumerate(messages)
                    if message.role == MessageRole.USER
                ]
                evidence_chain = (
                    messages[current_user_indexes[-1] :]
                    if current_user_indexes
                    else messages
                )
            user_messages = [
                message.content
                for message in evidence_chain
                if message.role == MessageRole.USER
            ]
            intent_context = [
                f"{message.role.value}: {message.content}" for message in evidence_chain
            ]
            intent = await self._intents.parse(
                request.question, intent_context, user_messages
            )
            try:
                intent.validate_evidence(user_messages)
            except ValueError as error:
                raise DataAgentError(
                    "query_intent_invalid",
                    "query_intent",
                    "查询意图缺少可验证的用户原文证据",
                    http_status=422,
                ) from error
            context_or_clarification = await self._metadata.build_context(
                " ".join(
                    [*intent.measure_quotes, *intent.dimension_quotes]
                    + [item.column_quote for item in intent.filters]
                    + [quote for item in intent.filters for quote in item.value_quotes]
                    + ([intent.time_quote] if intent.time_quote else [])
                    + ([intent.time_column_quote] if intent.time_column_quote else [])
                    + (
                        [
                            intent.time_filter.column_quote,
                            *intent.time_filter.value_quotes,
                        ]
                        if intent.time_filter
                        else []
                    )
                    + [item.quote for item in intent.sorts]
                )
                or request.question,
                intent,
                schema,
            )
            # 步骤四：绑定未唯一时只完成一个最高影响澄清，不进入 SQL 路径。
            if isinstance(context_or_clarification, QueryClarification):
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                await self._complete(
                    request,
                    context_or_clarification.question,
                    semantic_fingerprint="query:clarification",
                )
                yield QueryEvent(
                    kind="clarification", message=context_or_clarification.question
                )
                return
            context = context_or_clarification
            # 步骤五：一次生成和至多一次修复都必须重新经过 AST 与 EXPLAIN。
            validated = await self._plan(request, context, intent)
            if validated is None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                await self._complete(request, DATA_PREPARING_MESSAGE)
                yield QueryEvent(kind="complete", message=DATA_PREPARING_MESSAGE)
                return
            # 最终复核与完整结果读取共享同步 generation lock，避免 readiness
            # 通过后目标表切入重建并暴露空集或部分代次。
            async with self._readiness.hold(validated.target_tables):
                if not await self._metadata.relationships_are_authoritative(
                    context.physical_schema
                ):
                    raise DataAgentError(
                        "query_schema_changed",
                        "query_metadata",
                        "查询使用的物理模式已变化，请重试",
                        retryable=True,
                        http_status=409,
                    )
                if not await self._readiness.ready(validated.target_tables):
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                    await self._complete(request, DATA_PREPARING_MESSAGE)
                    yield QueryEvent(kind="complete", message=DATA_PREPARING_MESSAGE)
                    return
                await self._executor.explain(validated)
                # 步骤七：专用 SELECT-only executor 以单批预算流式读取，不施加总 LIMIT。
                started_at = perf_counter()
                row_count = 0
                sql_hash = hashlib.sha256(validated.sql.encode()).hexdigest()
                audit_identity = {
                    "user_id": request.user_id,
                    "conversation_uid": request.conversation_uid,
                    "turn_uid": request.turn_uid,
                    "sql_hash": sql_hash,
                    "table_ids": list(validated.table_ids),
                }
                structured_log(
                    "INFO",
                    "只读查询执行已开始",
                    **audit_identity,
                    outcome="started",
                    row_count=0,
                )
                stream = self._executor.execute(validated)
                try:
                    try:
                        first = await anext(stream)
                    except StopAsyncIteration:
                        first = None
                    columns = first.columns if first is not None else []
                    yield QueryEvent(
                        kind="metadata",
                        sql=validated.sql,
                        columns=columns,
                        result_scope="all_sources",
                    )
                    if first is not None and first.rows:
                        row_count += len(first.rows)
                        yield QueryEvent(kind="rows", rows=first.rows)
                    async for batch in stream:
                        row_count += len(batch.rows)
                        yield QueryEvent(kind="rows", rows=batch.rows)
                except BaseException as error:
                    elapsed_ms = round((perf_counter() - started_at) * 1000)
                    structured_log(
                        "WARNING",
                        "只读查询执行未完成",
                        **audit_identity,
                        outcome="failed",
                        row_count=row_count,
                        duration_ms=elapsed_ms,
                        error_type=type(error).__name__,
                    )
                    raise
                finally:
                    await stream.aclose()
                elapsed_ms = round((perf_counter() - started_at) * 1000)
                summary = f"查询完成，共返回 {row_count} 行。"
                structured_log(
                    "INFO",
                    "只读查询执行完成",
                    **audit_identity,
                    outcome="success",
                    row_count=row_count,
                    duration_ms=elapsed_ms,
                )
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                await self._complete(request, summary)
                yield QueryEvent(
                    kind="complete", row_count=row_count, elapsed_ms=elapsed_ms
                )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                await self._conversations.abandon_turn(
                    request.user_id,
                    request.conversation_uid,
                    request.turn_uid,
                )
            except Exception as error:
                logger.warning(
                    "查询轮次门禁释放失败：user_id={} conversation_uid={} "
                    "turn_uid={} error_type={}",
                    request.user_id,
                    request.conversation_uid,
                    request.turn_uid,
                    type(error).__name__,
                )

    async def _heartbeat_turn(
        self,
        request: QueryRequest,
        owner_task: asyncio.Task[object] | None,
    ) -> None:
        """独立续租健康长流；续租失败时 fence 掉旧执行者。"""
        interval = max(1.0, self._turn_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._conversations.renew_turn(
                    request.user_id, request.conversation_uid, request.turn_uid
                )
            except Exception:
                renewed = False
            if not renewed:
                if owner_task is not None:
                    owner_task.cancel()
                return

    async def _plan(
        self,
        request: QueryRequest,
        context: QueryContext,
        intent: QueryIntent,
    ) -> ValidatedQuery | None:
        """执行一次生成、静态门禁、EXPLAIN 和唯一修复闭环。"""
        draft = await self._planner.draft(context, intent)
        for attempt in range(2):
            result = await validate_query(
                draft,
                context,
                intent,
                dw_database=self._dw_database,
            )
            issues = result.issues
            if result.validated is not None:
                audit_identity = {
                    "user_id": request.user_id,
                    "conversation_uid": request.conversation_uid,
                    "turn_uid": request.turn_uid,
                    "sql_hash": hashlib.sha256(
                        result.validated.sql.encode()
                    ).hexdigest(),
                    "table_ids": list(result.validated.table_ids),
                }
                structured_log(
                    "INFO",
                    "只读查询预检已开始",
                    **audit_identity,
                    outcome="started",
                    row_count=0,
                )
                async with self._readiness.hold(result.validated.target_tables):
                    if not await self._metadata.relationships_are_authoritative(
                        context.physical_schema
                    ):
                        raise DataAgentError(
                            "query_schema_changed",
                            "query_metadata",
                            "查询使用的物理模式已变化，请重试",
                            retryable=True,
                            http_status=409,
                        )
                    if not await self._readiness.ready(
                        result.validated.target_tables
                    ):
                        structured_log(
                            "INFO",
                            "只读查询预检未执行",
                            **audit_identity,
                            outcome="not_ready",
                            row_count=0,
                        )
                        return None
                    try:
                        await self._executor.explain(result.validated)
                    except QueryExplainRejected as error:
                        structured_log(
                            "WARNING",
                            "只读查询预检未通过",
                            **audit_identity,
                            outcome="explain_rejected",
                            row_count=0,
                            error_type=type(error).__name__,
                        )
                        issues = (error.issue,)
                    except BaseException as error:
                        structured_log(
                            "WARNING",
                            "只读查询预检失败",
                            **audit_identity,
                            outcome="failed",
                            row_count=0,
                            error_type=type(error).__name__,
                        )
                        raise
                    else:
                        structured_log(
                            "INFO",
                            "只读查询预检通过",
                            **audit_identity,
                            outcome="explain_passed",
                            row_count=0,
                        )
                        return result.validated
            if attempt == 0:
                draft = await self._planner.repair(
                    context,
                    intent,
                    draft,
                    issues,
                )
                continue
            raise DataAgentError(
                "query_unsafe",
                "query_validation",
                "查询草稿两次未通过安全门禁",
                http_status=422,
                details={"issue_codes": ",".join(issue.code for issue in issues)},
            )
        raise AssertionError("查询修复循环必须返回或抛出")

    async def _complete(
        self,
        request: QueryRequest,
        content: str,
        *,
        semantic_fingerprint: str | None = None,
    ) -> None:
        """复用 Conversation 原子完成语义持久化助手文本。"""
        await self._conversations.complete_turn(
            request.user_id,
            request.conversation_uid,
            request.turn_uid,
            content,
            semantic_fingerprint=semantic_fingerprint,
        )
