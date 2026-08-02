"""DDL 任务 SSE HTTP 边界检查。"""

from datetime import UTC, datetime
from typing import cast

from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from application import create_app
from ddl_metadata.jobs.store import DDLJobStore
from errors import DataAgentError
from models.jobs import DDLJobRequest, JobRecord, JobStatus
from tests.helpers.checks import check_condition, check_equal


def _terminal_record() -> JobRecord:
    """构造会让 HTTP 流自然结束的终态记录。"""
    return JobRecord(
        job_id="job-1",
        source="source-1",
        status=JobStatus.REJECTED,
        attempt=1,
        created_at=datetime(2026, 7, 19, tzinfo=UTC),
        updated_at=datetime(2026, 7, 19, 0, 1, tzinfo=UTC),
    )


class _TerminalJobs:
    """提供终态 SSE 响应所需的应用门面。"""

    async def get(self, job_id: str) -> JobRecord:
        """返回终态公开投影。"""
        del job_id
        return _terminal_record()

    async def event_tail_id(self, job_id: str) -> str:
        """返回初始快照使用的尾游标。"""
        del job_id
        return "7-0"

    snapshot_event = staticmethod(DDLJobStore.snapshot_event)


class _MissingJobs:
    """模拟任务不存在。"""

    async def get(self, job_id: str) -> JobRecord:
        """抛出既有 404 业务错误。"""
        del job_id
        raise DataAgentError(
            "job_not_found",
            "job_status",
            "missing",
            http_status=404,
        )


class _UnavailableJobs:
    """模拟响应开始前 Redis 不可用。"""

    async def get(self, job_id: str) -> JobRecord:
        """抛出 Redis 连接错误。"""
        del job_id
        raise RedisConnectionError("private endpoint")


class _SubmitJobs:
    """模拟持久受理任务。"""

    submitted: DDLJobRequest | None = None

    async def submit(self, body: DDLJobRequest) -> JobRecord:
        """返回公开 Pending 记录。"""
        self.submitted = body
        return _terminal_record().model_copy(
            update={"status": JobStatus.PENDING}
        )


async def _get_with_jobs(jobs: object) -> tuple[int, dict[str, str], str]:
    """用替换后的任务门面请求 SSE 路由。"""
    app = create_app()
    app.state.jobs = cast(DDLJobStore, jobs)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/v1/metadata/ddl-jobs/job-1/events"
        )
    return response.status_code, dict(response.headers), response.text


async def test_sse_route_returns_standard_headers_and_terminal_snapshot() -> None:
    """HTTP 路由返回标准媒体类型、禁缓存头和终态快照。"""
    status_code, headers, body = await _get_with_jobs(_TerminalJobs())
    check_equal("SSE HTTP 状态", status_code, 200)
    check_condition(
        "SSE Content-Type",
        headers["content-type"].startswith("text/event-stream"),
        actual=headers["content-type"],
        expected="text/event-stream",
    )
    check_equal("SSE 禁缓存", headers["cache-control"], "no-cache")
    check_equal("SSE 禁代理缓冲", headers["x-accel-buffering"], "no")
    check_condition(
        "终态快照帧",
        body.startswith("id: 7-0\nevent: snapshot\ndata: {"),
        actual=body,
        expected="带尾游标的 snapshot",
    )


async def test_sse_route_preserves_404_and_pre_stream_503_mapping() -> None:
    """响应建立前复用既有任务 404 和 Redis 503 错误映射。"""
    missing_status, _, missing_body = await _get_with_jobs(_MissingJobs())
    check_equal("SSE 不存在状态码", missing_status, 404)
    check_condition(
        "SSE 不存在错误码",
        '"code":"job_not_found"' in missing_body,
        actual=missing_body,
        expected="既有 job_not_found",
    )
    unavailable_status, _, unavailable_body = await _get_with_jobs(
        _UnavailableJobs()
    )
    check_equal("SSE Redis 故障状态码", unavailable_status, 503)
    check_condition(
        "SSE Redis 安全错误",
        (
            '"code":"redis_unavailable"' in unavailable_body
            and "private endpoint" not in unavailable_body
        ),
        actual=unavailable_body,
        expected="固定 redis_unavailable 且无异常文本",
    )


async def test_submit_response_discovers_event_stream_url() -> None:
    """任务受理响应在保留旧字段的同时提供事件流 URL。"""
    app = create_app()
    app.state.jobs = cast(DDLJobStore, _SubmitJobs())
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/metadata/ddl-jobs",
            json={
                "source": "source-1",
                "ddl": "CREATE TABLE orders(id BIGINT)",
            },
        )
    payload = response.json()
    check_equal("受理响应状态", response.status_code, 202)
    check_equal(
        "既有状态 URL",
        payload["status_url"],
        "/api/v1/metadata/ddl-jobs/job-1",
    )
    check_equal(
        "可发现事件 URL",
        payload["events_url"],
        "/api/v1/metadata/ddl-jobs/job-1/events",
    )


async def test_submit_accepts_idempotency_coordinate_header() -> None:
    """新后端从可选头读取坐标，同时保持旧 JSON 请求体兼容。"""
    jobs = _SubmitJobs()
    app = create_app()
    app.state.jobs = cast(DDLJobStore, jobs)
    transport = ASGITransport(app=app)
    submission_id = "11111111-1111-4111-8111-111111111111"
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/metadata/ddl-jobs",
            headers={"Idempotency-Key": submission_id},
            json={"source": "source-1", "ddl": "CREATE TABLE orders(id BIGINT)"},
        )

    check_equal("幂等头受理状态", response.status_code, 202)
    check_equal(
        "幂等头投影到任务请求",
        jobs.submitted.submission_id if jobs.submitted else None,
        submission_id,
    )


def test_job_store_can_still_construct_with_redis_type() -> None:
    """保留 DDLJobStore 的 Redis 构造边界供应用生命周期使用。"""
    store = DDLJobStore(cast(Redis, object()))
    check_condition(
        "任务门面已构造",
        isinstance(store, DDLJobStore),
        actual=type(store).__name__,
        expected="DDLJobStore",
    )
