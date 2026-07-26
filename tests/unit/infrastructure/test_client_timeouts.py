"""外部服务客户端显式超时的生效检查。"""

from __future__ import annotations

import pytest

from data_agent.infrastructure import checkpoint_store as checkpoint_module
from data_agent.infrastructure.checkpoint_store import CheckpointStore
from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.infrastructure.redis import RedisClient
from data_agent.infrastructure.tei_embeddings import TEIEmbeddingClient
from data_agent.settings import app_config
from tests.helpers.checks import check_condition, check_equal


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
    from data_agent.ddl_metadata.worker.settings import WorkerSettings

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

    from data_agent.ddl_metadata.worker import settings as worker_settings

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
    from data_agent.ddl_metadata.worker import settings as worker_settings

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
