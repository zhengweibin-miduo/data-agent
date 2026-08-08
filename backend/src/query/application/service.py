"""自然语言只读查询的唯一应用编排入口。"""

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator, Awaitable
from datetime import UTC, datetime
from time import perf_counter
from typing import Callable, cast

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
    TrustedTimeRange,
    ValidatedQuery,
    resolve_trusted_time_range,
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
        now: Callable[[], datetime] | None = None,
        clarification_chain_message_limit: int = 100,
        clarification_chain_max_chars: int = 262_144,
        control_io_timeout_seconds: float = 10.0,
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
        self._now = now or (lambda: datetime.now(UTC))
        self._clarification_chain_message_limit = clarification_chain_message_limit
        self._clarification_chain_max_chars = clarification_chain_max_chars
        self._control_io_timeout_seconds = control_io_timeout_seconds

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
                        "user_timezone": request.supplemental_context.user_timezone,
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
            if started.execution_owner and started.claim_token is not None:
                await self._conversations.abandon_turn(
                    request.user_id,
                    request.conversation_uid,
                    request.turn_uid,
                    started.claim_token,
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
        claim_token = started.claim_token
        if claim_token is None:
            raise DataAgentError(
                "turn_claim_missing",
                "conversation_turn",
                "会话轮次未返回执行代次坐标",
                retryable=True,
                http_status=409,
            )
        owner_task = asyncio.current_task()
        heartbeat = asyncio.create_task(
            self._heartbeat_turn(request, claim_token, owner_task),
            name=f"query-turn-heartbeat:{request.turn_uid}",
        )
        try:
            # 步骤三：Query 证据链从 MySQL 权威消息独立读取，
            # 不依赖普通 Conversation 摘要后的小窗口。
            evidence_chain = await self._conversations.pending_query_chain(
                request.user_id,
                request.conversation_uid,
                through_id=started.message.id,
                message_limit=self._clarification_chain_message_limit,
                max_chars=self._clarification_chain_max_chars,
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
                    claim_token,
                    context_or_clarification.question,
                    semantic_fingerprint="query:clarification",
                )
                yield QueryEvent(
                    kind="clarification", message=context_or_clarification.question
                )
                return
            context = context_or_clarification.model_copy(
                update={
                    "user_timezone": request.supplemental_context.user_timezone
                }
            )
            await self._ensure_timezone_supported(context, intent)
            trusted_time_range = self._trusted_time_range(request, context, intent)
            # 步骤五：一次生成和至多一次修复都必须重新经过 AST 与 EXPLAIN。
            validated = await self._plan(request, context, intent, trusted_time_range)
            if validated is None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                await self._complete(request, claim_token, DATA_PREPARING_MESSAGE)
                yield QueryEvent(kind="complete", message=DATA_PREPARING_MESSAGE)
                return
            # 最终复核与完整结果读取共享同步 generation lock，避免 readiness
            # 通过后目标表切入重建并暴露空集或部分代次。
            async with self._readiness.hold(validated.target_tables):
                if not await self._control_read(
                    self._metadata.relationships_are_authoritative(
                        context.physical_schema
                    )
                ):
                    raise DataAgentError(
                        "query_schema_changed",
                        "query_metadata",
                        "查询使用的物理模式已变化，请重试",
                        retryable=True,
                        http_status=409,
                    )
                if not await self._control_read(
                    self._metadata.bindings_are_authoritative(context)
                ):
                    raise DataAgentError(
                        "query_metadata_changed",
                        "query_metadata",
                        "查询使用的业务语义绑定已变化，请重试",
                        retryable=True,
                        http_status=409,
                    )
                if not await self._control_read(
                    self._readiness.ready(validated.target_tables)
                ):
                    heartbeat.cancel()
                    await asyncio.gather(heartbeat, return_exceptions=True)
                    await self._complete(request, claim_token, DATA_PREPARING_MESSAGE)
                    yield QueryEvent(kind="complete", message=DATA_PREPARING_MESSAGE)
                    return
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
                stream = None
                try:
                    await self._executor.explain(validated)
                    # 步骤七：专用 SELECT-only executor 流式读取，
                    # 不施加总 LIMIT。
                    stream = self._executor.execute(validated)
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
                    if stream is not None:
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
                await self._complete(request, claim_token, summary)
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
                    claim_token,
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
        claim_token: str,
        owner_task: asyncio.Task[object] | None,
    ) -> None:
        """独立续租健康长流；续租失败时 fence 掉旧执行者。"""
        interval = max(1.0, self._turn_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                renewed = await self._conversations.renew_turn(
                    request.user_id,
                    request.conversation_uid,
                    request.turn_uid,
                    claim_token,
                )
            except Exception as error:
                logger.warning(
                    "查询轮次续租暂时失败：user_id={} conversation_uid={} "
                    "turn_uid={} error_type={}",
                    request.user_id,
                    request.conversation_uid,
                    request.turn_uid,
                    type(error).__name__,
                )
                continue
            if not renewed:
                if owner_task is not None:
                    owner_task.cancel("query_lease_lost")
                return

    async def _plan(
        self,
        request: QueryRequest,
        context: QueryContext,
        intent: QueryIntent,
        trusted_time_range: TrustedTimeRange | None,
    ) -> ValidatedQuery | None:
        """执行一次生成、静态门禁、EXPLAIN 和唯一修复闭环。"""
        draft = await self._planner.draft(context, intent, trusted_time_range)
        for attempt in range(2):
            result = await validate_query(
                draft,
                context,
                intent,
                trusted_time_range=trusted_time_range,
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
                    if not await self._control_read(
                        self._metadata.relationships_are_authoritative(
                            context.physical_schema
                        )
                    ):
                        raise DataAgentError(
                            "query_schema_changed",
                            "query_metadata",
                            "查询使用的物理模式已变化，请重试",
                            retryable=True,
                            http_status=409,
                        )
                    if not await self._control_read(
                        self._metadata.bindings_are_authoritative(context)
                    ):
                        raise DataAgentError(
                            "query_metadata_changed",
                            "query_metadata",
                            "查询使用的业务语义绑定已变化，请重试",
                            retryable=True,
                            http_status=409,
                        )
                    if not await self._control_read(
                        self._readiness.ready(result.validated.target_tables)
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
                    trusted_time_range,
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

    async def _control_read(self, awaitable: Awaitable[object]) -> object:
        """限制 generation READ 锁内控制面数据库读取时长。"""
        try:
            async with asyncio.timeout(self._control_io_timeout_seconds):
                return await awaitable
        except TimeoutError as error:
            raise DataAgentError(
                "query_control_timeout",
                "query_readiness",
                "查询权威状态复核超时，请稍后重试",
                retryable=True,
                http_status=503,
            ) from error

    async def _ensure_timezone_supported(
        self, context: QueryContext, intent: QueryIntent
    ) -> None:
        """在生成 CONVERT_TZ 前证明执行实例具备 named-zone 数据。"""
        if intent.grain is None or intent.time_column_quote is None:
            return
        column_id = context.bindings.get(intent.time_column_quote)
        column = next(
            (
                column
                for table in context.physical_schema.tables
                for column in table.columns
                if column.id == column_id
            ),
            None,
        )
        if column is None or not column.data_type.upper().startswith("TIMESTAMP"):
            return
        ensure = cast(
            Callable[[str], Awaitable[None]] | None,
            getattr(self._executor, "ensure_timezone_supported", None),
        )
        if callable(ensure):
            await ensure(context.user_timezone)

    def _trusted_time_range(
        self,
        request: QueryRequest,
        context: QueryContext,
        intent: QueryIntent,
    ) -> TrustedTimeRange | None:
        """在 Meta 绑定后仅用权威字段类型派生自然时间边界。"""
        if intent.time_quote is None or intent.time_filter is not None:
            return None
        if intent.time_column_quote is None:
            raise DataAgentError(
                "query_time_unsupported",
                "query_validation",
                "时间范围缺少已绑定的时间字段",
                http_status=422,
            )
        column_id = context.bindings.get(intent.time_column_quote)
        column = next(
            (
                column
                for table in context.physical_schema.tables
                for column in table.columns
                if column.id == column_id
            ),
            None,
        )
        if column is None:
            raise DataAgentError(
                "query_time_unsupported",
                "query_validation",
                "时间范围无法绑定到权威时间字段",
                http_status=422,
            )
        trusted = resolve_trusted_time_range(
            source_quote=intent.time_quote,
            column_id=column.id,
            column_name=column.name,
            data_type=column.data_type,
            user_timezone=request.supplemental_context.user_timezone,
            now_utc=self._now(),
        )
        if trusted is None:
            raise DataAgentError(
                "query_time_unsupported",
                "query_validation",
                "时间范围或时间字段类型不受支持",
                http_status=422,
            )
        return trusted

    async def _complete(
        self,
        request: QueryRequest,
        claim_token: str,
        content: str,
        *,
        semantic_fingerprint: str | None = None,
    ) -> None:
        """复用 Conversation 原子完成语义持久化助手文本。"""
        await self._conversations.complete_turn(
            request.user_id,
            request.conversation_uid,
            request.turn_uid,
            claim_token,
            content,
            semantic_fingerprint=semantic_fingerprint,
        )
