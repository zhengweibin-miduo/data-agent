"""外部服务客户端显式超时的生效检查。"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest
from redis.asyncio.connection import AbstractConnection
from redis.exceptions import ConnectionError as RedisConnectionError

from infrastructure import checkpoint_store as checkpoint_module
from infrastructure.checkpoint_store import CheckpointStore
from infrastructure.elasticsearch import ElasticsearchClient
from infrastructure.qdrant import QdrantClient
from infrastructure.redis import RedisClient
from infrastructure.tei_embeddings import TEIEmbeddingClient
from settings import app_config
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)


async def _noop_close() -> None:
    """生命周期关闭替身，不触碰真实外部资源。"""


async def test_redis_client_declares_socket_timeouts() -> None:
    """Redis 必须声明读取与连接超时，避免半开连接让调用方永久挂起。"""
    await RedisClient.close()
    client = RedisClient.initialize()
    try:
        kwargs = client.connection_pool.connection_kwargs
        check_equal(
            "Redis 读取超时",
            kwargs.get("socket_timeout"),
            app_config.redis.socket_timeout_seconds,
        )
        check_equal(
            "Redis 连接超时",
            kwargs.get("socket_connect_timeout"),
            app_config.redis.socket_connect_timeout_seconds,
        )
        check_equal(
            "Redis 健康检查间隔",
            kwargs.get("health_check_interval"),
            app_config.redis.health_check_interval_seconds,
        )
    finally:
        await RedisClient.close()


async def test_redis_socket_timeout_outlives_sse_heartbeat() -> None:
    """SSE 空闲心跳会阻塞满一个间隔，读取超时必须严格大于它。"""
    check_equal(
        "读取超时严格大于 SSE 心跳间隔",
        app_config.redis.socket_timeout_seconds > app_config.api.sse_heartbeat_seconds,
        True,
    )


async def test_elasticsearch_client_declares_timeout_and_bounded_retry() -> None:
    """Elasticsearch 读路径没有 outbox 兜底，必须有超时与有界重试。"""
    await ElasticsearchClient.close()
    client = ElasticsearchClient.initialize()
    try:
        check_equal(
            "Elasticsearch 请求超时",
            client._request_timeout,
            app_config.elasticsearch.request_timeout_seconds,
        )
        check_equal(
            "Elasticsearch 最大重试次数",
            client._max_retries,
            app_config.elasticsearch.max_retries,
        )
        check_equal("Elasticsearch 超时后重试", client._retry_on_timeout, True)
    finally:
        await ElasticsearchClient.close()


async def test_qdrant_client_declares_timeout() -> None:
    """Qdrant 必须声明请求超时，重试交给记忆索引 outbox 退避。"""
    await QdrantClient.close()
    client = QdrantClient.initialize()
    try:
        # 超时只在远程实现上保留私有字段，按属性名读取真实生效值。
        check_equal(
            "Qdrant 请求超时",
            getattr(client._client, "_timeout", None),
            app_config.qdrant.timeout_seconds,
        )
    finally:
        await QdrantClient.close()


async def test_tei_client_declares_timeout() -> None:
    """TEI 向量化必须声明超时，否则挂起会占满索引调度周期。"""
    await TEIEmbeddingClient.close()
    client = TEIEmbeddingClient.initialize()
    try:
        check_equal(
            "TEI 请求超时",
            client.async_client.timeout,
            app_config.tei.request_timeout_seconds,
        )
    finally:
        await TEIEmbeddingClient.close()


async def test_checkpoint_saver_receives_socket_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """检查点 saver 自建连接池，必须单独注入同一组套接字超时。"""
    captured: dict[str, object] = {}

    class FakeSaver:
        """记录构造参数并跳过真实连接的 saver 替身。"""

        def __init__(self, redis_url: str, **kwargs: object) -> None:
            """记录 URL 与连接参数。"""
            captured["url"] = redis_url
            captured.update(kwargs)

        async def __aenter__(self) -> FakeSaver:
            """不建立真实连接。"""
            return self

        async def __aexit__(self, *args: object) -> None:
            """不释放真实连接。"""

        async def asetup(self) -> None:
            """跳过索引初始化。"""

    monkeypatch.setattr(checkpoint_module, "AsyncRedisSaver", FakeSaver)
    await CheckpointStore.close()
    try:
        await CheckpointStore.initialize()
        connection_args = captured.get("connection_args")
        check_condition(
            "注入了连接参数",
            isinstance(connection_args, dict),
            actual=captured,
            expected="构造时传入 connection_args",
        )
        assert isinstance(connection_args, dict)
        check_equal(
            "检查点读取超时",
            connection_args.get("socket_timeout"),
            app_config.redis.socket_timeout_seconds,
        )
        check_equal(
            "检查点连接超时",
            connection_args.get("socket_connect_timeout"),
            app_config.redis.socket_connect_timeout_seconds,
        )
    finally:
        await CheckpointStore.close()


async def test_worker_queue_pool_declares_socket_timeouts() -> None:
    """Worker 自己的 arq 队列客户端也必须带读取超时。"""
    # arq.create_pool 只把 conn_timeout 映射为 socket_connect_timeout，从不设置
    # socket_timeout；半开连接会让队列轮询无限等待，整个 worker 停止领取任务。
    from ddl_metadata.worker.settings import WorkerSettings

    kwargs = WorkerSettings.redis_pool.connection_pool.connection_kwargs
    check_equal(
        "队列读取超时",
        kwargs.get("socket_timeout"),
        app_config.redis.socket_timeout_seconds,
    )
    check_equal(
        "队列连接超时",
        kwargs.get("socket_connect_timeout"),
        app_config.redis.socket_connect_timeout_seconds,
    )
    check_equal(
        "队列健康检查间隔",
        kwargs.get("health_check_interval"),
        app_config.redis.health_check_interval_seconds,
    )
    # 队列语义必须与 arq 默认一致，否则 worker 会去轮询另一个队列。
    check_equal(
        "沿用 arq 默认队列名", WorkerSettings.redis_pool.default_queue_name, "arq:queue"
    )
    check_equal("沿用 DSN 解析出的库编号", kwargs.get("db"), 0)


async def test_worker_queue_settings_survive_non_default_dsn() -> None:
    """非默认 DSN 下队列池与健康探针都必须指向真实队列。"""
    from arq.connections import RedisSettings

    from ddl_metadata.worker import settings as worker_settings

    # `arq ... --check` 只读 redis_settings、不读 redis_pool；缺省时会去连
    # localhost:6379/0 而不是配置指向的队列，可能把正常 worker 判死。
    expected = RedisSettings.from_dsn(app_config.redis.url)
    actual = worker_settings.WorkerSettings.redis_settings
    check_equal("健康探针主机", actual.host, expected.host)
    check_equal("健康探针端口", actual.port, expected.port)
    check_equal("健康探针库编号", actual.database, expected.database)


async def test_worker_queue_pool_uses_unix_socket_when_dsn_requires_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """unix:// DSN 必须走 Unix domain socket，而不是回落到 TCP。"""
    from ddl_metadata.worker import settings as worker_settings

    dsn = "unix:///tmp/data-agent-redis.sock"
    monkeypatch.setattr(
        worker_settings.app_config.redis,
        "url",
        dsn,
        raising=False,
    )
    pool = worker_settings._queue_pool()
    try:
        kwargs = pool.connection_pool.connection_kwargs
        check_equal(
            "使用 DSN 中的 socket 路径",
            kwargs.get("path"),
            "/tmp/data-agent-redis.sock",
        )
        check_equal("不再回落到 TCP 主机", kwargs.get("host"), None)
        check_equal(
            "socket 连接同样带读取超时",
            kwargs.get("socket_timeout"),
            app_config.redis.socket_timeout_seconds,
        )
    finally:
        await pool.aclose()


async def test_api_queue_pool_declares_socket_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API 自己的 arq 队列池是本进程族的第四个 Redis 客户端，同样要带读取超时。"""
    # 缺少读取超时时，半开的队列连接会让 submit 停在 _activate_now_safely() ->
    # dispatch_one() -> queue.enqueue_job() 上永不返回：既没有 HTTP 响应，异常处理
    # 器也无法退回 cron 调度，因为"永不返回"的调用不会抛出任何异常。
    from fastapi import FastAPI

    import application

    monkeypatch.setattr(application, "setup_logging", lambda: None)
    for manager in (
        application.RedisClient,
        application.MySQLDatabase,
        application.ElasticsearchClient,
        application.QdrantClient,
        application.TEIEmbeddingClient,
        application.LLMClient,
    ):
        monkeypatch.setattr(manager, "initialize", lambda: object())
        monkeypatch.setattr(manager, "close", _noop_close)
    monkeypatch.setattr(
        application.MySQLDatabase,
        "check_locking_service",
        AsyncMock(),
    )

    app = FastAPI()
    async with application._lifespan(app):
        # 从真实装配结果读取队列，确保断言的是生命周期实际交给 DDLJobStore 的那个池。
        kwargs = app.state.jobs._queue.connection_pool.connection_kwargs
        check_equal(
            "API 队列读取超时",
            kwargs.get("socket_timeout"),
            app_config.redis.socket_timeout_seconds,
        )
        check_equal(
            "API 队列连接超时",
            kwargs.get("socket_connect_timeout"),
            app_config.redis.socket_connect_timeout_seconds,
        )
        check_equal(
            "API 队列健康检查间隔",
            kwargs.get("health_check_interval"),
            app_config.redis.health_check_interval_seconds,
        )


async def test_queue_pool_retry_covers_connect_phase_only() -> None:
    """连接重试判定必须覆盖原始套接字异常，且排除读取超时。"""
    # redis-py 的 connect() 把 _connect() 包在 retry 里，但 OSError -> ConnectionError
    # 与 asyncio.TimeoutError -> TimeoutError 两处转换都在 call_with_retry **之外**，
    # 因此重试判定看到的是原始 OSError。Retry 默认 supported_errors 只有 redis 自己的
    # ConnectionError/TimeoutError，连接阶段一次都不会重试——这正是要修的缺陷。
    import socket

    from redis.exceptions import TimeoutError as RedisTimeoutError

    from infrastructure import job_queue

    pool = job_queue.build_queue_pool(
        connect_retries=job_queue.worker_connect_retries()
    )
    try:
        # 断言必须落在**连接实例**上，而不是构造时传入的 Retry 或模块常量：
        # `Connection.__init__` 会 deepcopy 该 Retry 再执行
        # `update_supported_errors(retry_on_error)`，这是并集追加、不做剔除。只断言传入
        # 值的测试挡不住"有人往 retry_on_error 里补了 TimeoutError"——那会让读取超时重新
        # 进入重试，把一次挂起放大成多次。
        # make_connection 的声明返回类型是 ConnectionInterface，重试策略挂在具体实现
        # AbstractConnection 上，因此按真实类型收窄后再读取。
        connection = cast(
            AbstractConnection,
            pool.connection_pool.make_connection(),
        )
        supported = connection.retry._supported_errors
        for label, error in (
            ("连接被拒", ConnectionRefusedError()),
            ("连接超时", TimeoutError()),
            ("套接字超时", socket.timeout()),
            ("重连失败", RedisConnectionError()),
        ):
            check_condition(
                f"{label} 进入连接重试",
                isinstance(error, supported),
                actual=type(error),
                expected=f"属于 {supported}",
            )
        # 读取超时是"服务收下命令后静默"，重试只会把一次挂起变成多次挂起。
        check_condition(
            "读取超时不进入连接重试",
            isinstance(RedisTimeoutError(), supported) is False,
            actual=supported,
            expected="不含 redis TimeoutError",
        )
    finally:
        await pool.aclose()


async def test_queue_pool_retries_unready_connection_within_budget() -> None:
    """未就绪的连接目标必须按预算重试，而不是首次失败即放弃。"""
    from redis.backoff import NoBackoff

    from infrastructure.job_queue import (
        build_queue_pool,
        worker_connect_retries,
    )

    retries = worker_connect_retries()
    pool = build_queue_pool(connect_retries=retries)
    try:
        retry = pool.connection_pool.connection_kwargs["retry"]
        check_equal("重试次数沿用 conn_retries", retry._retries, retries)
        # 退避改为零延迟只为让断言快速收敛，重试判定与次数保持真实配置。
        retry._backoff = NoBackoff()
        attempts = {"count": 0}

        async def refuse_until_ready() -> str:
            """前若干次以真实的连接被拒异常失败，随后成功。"""
            attempts["count"] += 1
            if attempts["count"] <= retries:
                raise ConnectionRefusedError(10061, "目标机器积极拒绝")
            return "connected"

        async def drop(error: BaseException) -> None:
            """连接阶段的失败回调只断开连接，不改变重试判定。"""
            del error

        check_equal(
            "耗尽预算前恢复即成功",
            await retry.call_with_retry(refuse_until_ready, drop),
            "connected",
        )
        check_equal("用满预算内的全部尝试", attempts["count"], retries + 1)
    finally:
        await pool.aclose()


async def test_queue_pool_fails_read_timeout_after_single_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """服务收下命令后静默时，读取超时只能失败一次，不得被放大成多次挂起。"""
    import asyncio
    import time

    from redis.exceptions import TimeoutError as RedisTimeoutError

    from infrastructure import job_queue

    read_timeout = 0.3
    connections = {"count": 0}
    # 处理器必须能被收尾唤醒：Python 3.12.1 起 Server.wait_closed() 会等待所有已建立
    # 连接的处理器结束，处理器里挂死会让整个测试进程停在收尾上。
    released = asyncio.Event()

    async def silent(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """接受连接并收下命令，但在断言完成前不响应。"""
        connections["count"] += 1
        await reader.read(100)
        await released.wait()
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(
        job_queue.app_config.redis,
        "url",
        f"redis://127.0.0.1:{port}/0",
        raising=False,
    )
    monkeypatch.setattr(
        job_queue.app_config.redis,
        "socket_timeout_seconds",
        read_timeout,
        raising=False,
    )
    pool = job_queue.build_queue_pool(
        connect_retries=job_queue.worker_connect_retries()
    )
    try:
        started = time.monotonic()
        try:
            await pool.ping()
        except RedisTimeoutError as error:
            check_exception("读取超时如实抛出", error, RedisTimeoutError)
        else:
            fail_check(
                "静默服务读取",
                actual="未抛出异常",
                expected="抛出读取超时",
            )
        elapsed = time.monotonic() - started
        check_equal("只建立一次连接", connections["count"], 1)
        check_condition(
            "耗时不超过单次读取超时预算",
            elapsed < read_timeout * 3,
            actual=elapsed,
            expected=f"小于 {read_timeout * 3} 秒",
        )
    finally:
        released.set()
        await pool.aclose()
        server.close()
        await server.wait_closed()


async def test_arq_worker_issues_redis_command_before_on_startup() -> None:
    """锁定 arq 启动顺序：首次 Redis 命令早于 on_startup。"""
    # 这是上一条测试的存在理由。若 arq 升级后改成先调用 on_startup，连通性等待就能
    # 重新兜住首次命令，本组约束可以放宽；在那之前重试预算必须留在池上。
    import inspect

    from arq.worker import Worker

    source = inspect.getsource(Worker.main)
    check_condition(
        "create_pool 仅在未提供 redis_pool 时执行",
        "if self._pool is None:" in source and "create_pool(" in source,
        actual=source,
        expected="create_pool 被 _pool is None 守卫",
    )
    check_condition(
        "log_redis_info 早于 on_startup",
        source.index("log_redis_info") < source.index("self.on_startup"),
        actual=source,
        expected="首次 Redis 命令在 on_startup 之前",
    )
