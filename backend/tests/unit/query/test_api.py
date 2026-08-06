"""自然语言查询 NDJSON HTTP seam 测试。"""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from errors import DataAgentError
from query.adapters.http import _DeadlineStreamingResponse, router
from query.application.contracts import QueryEvent


class _Query:
    """返回两个可观察 NDJSON 事件。"""

    async def stream(self, _request: object):
        """产生 metadata 和 complete 事件。"""
        yield QueryEvent(kind="metadata", sql="SELECT 1", columns=["value"])
        yield QueryEvent(kind="complete", row_count=0, elapsed_ms=1)


class _FailingQuery:
    """在响应开始后抛出安全业务错误。"""

    async def stream(self, _request: object):
        """先产生元数据，再模拟执行失败。"""
        yield QueryEvent(kind="metadata", sql="SELECT 1", columns=["value"])
        raise DataAgentError(
            "query_timeout",
            "query_execute",
            "执行超时",
            retryable=True,
            http_status=504,
        )


class _PreResponseStream:
    """记录首事件失败时 HTTP 适配器是否关闭流。"""

    closed = False

    def __aiter__(self):
        """返回当前异步迭代器。"""
        return self

    async def __anext__(self) -> QueryEvent:
        """在响应开始前抛出错误。"""
        raise RuntimeError("pre-response failure")

    async def aclose(self) -> None:
        """记录确定性关闭。"""
        self.closed = True


class _PreResponseFailingQuery:
    """返回首事件失败的可关闭流。"""

    def __init__(self) -> None:
        self.result = _PreResponseStream()

    def stream(self, _request: object) -> _PreResponseStream:
        """返回测试流。"""
        return self.result


async def test_query_route_streams_one_json_object_per_line() -> None:
    """独立 query-turns 路由返回严格 NDJSON 且拒绝未知字段。"""
    app = FastAPI()
    app.state.query = _Query()
    app.include_router(router)
    transport = httpx.ASGITransport(app=app)
    body = {
        "user_id": "user-1",
        "turn_uid": "turn-1",
        "question": "查询订单",
        "ddl_context": {
            "source": "erp",
            "dialect": "mysql",
            "ddl": "CREATE TABLE orders (id BIGINT)",
        },
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/conversations/conversation-1/query-turns",
            json=body,
        )
        invalid = await client.post(
            "/api/v1/conversations/conversation-1/query-turns",
            json={**body, "unexpected": True},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert [json.loads(line)["kind"] for line in response.text.splitlines()] == [
        "metadata",
        "complete",
    ]
    assert invalid.status_code == 422


async def test_query_route_handles_stream_errors_and_cleanup() -> None:
    """响应后错误固定投影，响应前失败也确定性关闭应用流。"""
    body = {
        "user_id": "user-1",
        "turn_uid": "turn-1",
        "question": "查询订单",
        "ddl_context": {
            "source": "erp",
            "dialect": "mysql",
            "ddl": "CREATE TABLE orders (id BIGINT)",
        },
    }
    app = FastAPI()
    app.state.query = _FailingQuery()
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/conversations/conversation-1/query-turns", json=body
        )
    events = [json.loads(line) for line in response.text.splitlines()]
    assert [event["kind"] for event in events] == ["metadata", "stream_error"]
    assert events[-1]["error"] == {
        "code": "query_timeout",
        "stage": "query_execute",
        "retryable": True,
    }

    pre_response = _PreResponseFailingQuery()
    failed_app = FastAPI()
    failed_app.state.query = pre_response
    failed_app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=failed_app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        failed = await client.post(
            "/api/v1/conversations/conversation-1/query-turns", json=body
        )
    assert failed.status_code == 500
    assert pre_response.result.closed is True


async def test_streaming_deadline_closes_application_stream_during_send_backpressure(
) -> None:
    """客户端停止接收时，壁钟预算仍会关闭流并释放应用资源。"""
    closed = asyncio.Event()

    async def content():
        try:
            yield b'{"kind":"metadata"}\n'
            yield b'{"kind":"rows"}\n'
        finally:
            closed.set()

    body_started = asyncio.Event()

    async def blocked_send(message: object) -> None:
        if isinstance(message, dict) and message.get("type") == "http.response.body":
            body_started.set()
            await asyncio.Event().wait()

    response = _DeadlineStreamingResponse(
        content(),
        timeout_seconds=0.01,
        media_type="application/x-ndjson",
    )

    with pytest.raises(TimeoutError):
        await response.stream_response(blocked_send)

    assert body_started.is_set()
    assert closed.is_set()
