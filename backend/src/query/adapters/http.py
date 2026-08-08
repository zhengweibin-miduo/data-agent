"""自然语言查询的 NDJSON HTTP 适配器。"""

import asyncio
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import Field

from errors import DataAgentError
from models.base import ContractModel
from models.jobs import DDLJobRequest
from query.application.contracts import (
    QueryEvent,
    QueryRequest,
    QueryStreamError,
    SupplementalQueryContext,
)
from query.application.service import QueryApplication
from settings import app_config

router = APIRouter(prefix="/api/v1/conversations", tags=["query"])


class QueryTurnRequest(ContractModel):
    """在当前 DDL 上执行一轮自然语言只读查询。"""

    user_id: str = Field(min_length=1, max_length=128, description="用户标识。")
    turn_uid: str = Field(min_length=1, max_length=64, description="轮次唯一标识。")
    question: str = Field(
        min_length=1,
        max_length=app_config.conversation.max_message_chars,
        description="用户查询原文。",
    )
    supplemental_context: SupplementalQueryContext = Field(
        description="包含显式 IANA 用户时区的 Query 补充上下文。"
    )
    ddl_context: DDLJobRequest = Field(
        description="用于元数据绑定的当前来源和 MySQL DDL；不按来源过滤业务结果行。"
    )


def _line(event: QueryEvent) -> bytes:
    """把一个类型化事件编码为单行 UTF-8 NDJSON。"""
    return (event.model_dump_json(exclude_none=True) + "\n").encode()


def _internal_cancellation(
    error: asyncio.CancelledError,
) -> tuple[str, str, str] | None:
    """把内部 fencing 取消原因映射为稳定的 Query 错误坐标。"""
    if len(error.args) != 1 or not isinstance(error.args[0], str):
        return None
    projections = {
        "query_lease_lost": ("conversation_turn", "查询轮次执行权已失效"),
        "generation_lock_owner_lost": (
            "query_readiness",
            "查询代次锁执行权已失效",
        ),
    }
    projection = projections.get(error.args[0])
    if projection is None:
        return None
    return error.args[0], *projection


async def _remaining(
    first: QueryEvent,
    stream: AsyncGenerator[QueryEvent, None],
) -> AsyncGenerator[bytes, None]:
    """发送已预取首事件并把响应后的异常收敛为固定安全事件。"""
    try:
        yield _line(first)
        async for event in stream:
            yield _line(event)
    except DataAgentError as error:
        yield _line(
            QueryEvent(
                kind="stream_error",
                error=QueryStreamError(
                    code=error.code,
                    stage=error.stage,
                    retryable=error.retryable,
                ),
            )
        )
    except asyncio.CancelledError as error:
        projection = _internal_cancellation(error)
        if projection is None:
            raise
        reason, stage, _message = projection
        yield _line(
            QueryEvent(
                kind="stream_error",
                error=QueryStreamError(
                    code=reason,
                    stage=stage,
                    retryable=True,
                ),
            )
        )
    except Exception:
        yield _line(
            QueryEvent(
                kind="stream_error",
                error=QueryStreamError(
                    code="query_stream_failed",
                    stage="query_stream",
                    retryable=True,
                ),
            )
        )
    finally:
        await stream.aclose()


@router.post("/{conversation_uid}/query-turns")
async def query_turn(
    conversation_uid: str,
    body: QueryTurnRequest,
    request: Request,
) -> StreamingResponse:
    """预取首事件后，以 NDJSON 流返回澄清或完整查询结果。"""
    application: QueryApplication = request.app.state.query
    stream = application.stream(
        QueryRequest(
            user_id=body.user_id,
            conversation_uid=conversation_uid,
            turn_uid=body.turn_uid,
            question=body.question,
            supplemental_context=body.supplemental_context,
            ddl_context=body.ddl_context,
        )
    )
    # 步骤一：首事件在响应开始前执行，使输入、意图和首个门禁错误仍走中央 HTTP 映射。
    try:
        first = await anext(stream)
    except StopAsyncIteration as error:
        await stream.aclose()
        raise DataAgentError(
            "query_empty_stream",
            "query_stream",
            "查询应用未产生任何流事件",
            retryable=True,
            http_status=502,
        ) from error
    except asyncio.CancelledError as error:
        await stream.aclose()
        projection = _internal_cancellation(error)
        if projection is None:
            raise
        reason, stage, message = projection
        raise DataAgentError(
            reason,
            stage,
            message,
            retryable=True,
            http_status=409,
        ) from error
    except BaseException:
        await stream.aclose()
        raise
    # 步骤二：响应开始后只发送类型化事件或固定安全 stream_error。
    return StreamingResponse(
        _remaining(first, stream),
        media_type="application/x-ndjson",
    )
