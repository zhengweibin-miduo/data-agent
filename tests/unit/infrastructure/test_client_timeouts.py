"""外部服务客户端显式超时的生效检查。"""

from __future__ import annotations

from data_agent.infrastructure.elasticsearch import ElasticsearchClient
from data_agent.infrastructure.qdrant import QdrantClient
from data_agent.infrastructure.redis import RedisClient
from data_agent.infrastructure.tei_embeddings import TEIEmbeddingClient
from data_agent.settings import app_config
from tests.helpers.checks import check_equal


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
