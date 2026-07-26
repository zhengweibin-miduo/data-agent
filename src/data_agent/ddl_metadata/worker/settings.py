"""arq worker 发现设置。"""

from collections.abc import Callable
from typing import Any

from arq import cron, func
from arq.connections import ArqRedis, RedisSettings
from arq.constants import default_queue_name, expires_extra_ms

from data_agent.ddl_metadata.worker.job_runner import run_ddl_job
from data_agent.ddl_metadata.worker.lifecycle import shutdown, startup
from data_agent.ddl_metadata.worker.maintenance import (
    cleanup_checkpoints,
    dispatch_memory_index_outbox,
    dispatch_pending,
    expire_memories,
    expire_waiting,
    extract_conversation_memory,
    purge_user_memories,
    reap_stalled_jobs,
)
from data_agent.logging import logging_boundary
from data_agent.settings import app_config


def _observed(function: Callable[..., Any]) -> Callable[..., Any]:
    """在 arq 注册 seam 外部织入零配置日志边界。"""
    return logging_boundary()(function)


def _queue_pool() -> ArqRedis:
    """构造带显式读取超时的 arq 队列客户端。

    `arq.create_pool` 只把 `RedisSettings.conn_timeout` 映射到
    `socket_connect_timeout`，从不设置 `socket_timeout`。因此既有连接半开、或 Redis
    收下命令后不再响应时，worker 的队列轮询会无限等待，整个 worker 停止领取任务与
    运行维护循环——这正是本次超时改动要消除的故障，却唯独漏掉了 worker 自己的队列
    客户端（`RedisClient` 与 `AsyncRedisSaver` 都已单独注入）。

    这里按 arq 自身的构造方式建池并补上读取超时。arq worker 的队列轮询是非阻塞的
    （`poll_delay` 默认 0.5 秒，源码中没有 BLPOP/XREAD 之类的阻塞读取），因此统一的
    读取超时不会误伤正常空闲等待。

    Returns:
        已设置 arq 运行期属性的队列客户端。
    """
    # 步骤一：沿用 DSN 解析出的连接参数，只补上 arq 不会设置的读取与健康检查配置。
    settings = RedisSettings.from_dsn(app_config.redis.url)
    # `unix://` DSN 会被解析进 unix_socket_path；漏传它会让 worker 连回 TCP
    # localhost:6379，与 API 客户端连到不同实例，全部任务领取与维护 cron 失效。
    connection: dict[str, Any] = (
        {"unix_socket_path": settings.unix_socket_path}
        if settings.unix_socket_path
        else {"host": settings.host, "port": settings.port}
    )
    pool = ArqRedis(
        **connection,
        db=settings.database,
        username=settings.username,
        password=settings.password,
        ssl=settings.ssl,
        encoding="utf8",
        max_connections=settings.max_connections,
        retry=settings.retry,
        retry_on_timeout=settings.retry_on_timeout,
        retry_on_error=settings.retry_on_error,
        socket_connect_timeout=app_config.redis.socket_connect_timeout_seconds,
        socket_timeout=app_config.redis.socket_timeout_seconds,
        health_check_interval=app_config.redis.health_check_interval_seconds,
    )
    # 步骤二：补齐 create_pool 会设置的运行期属性，保持队列语义与 arq 默认一致。
    pool.job_serializer = None
    pool.job_deserializer = None
    pool.default_queue_name = default_queue_name
    pool.expires_extra_ms = expires_extra_ms
    return pool


class WorkerSettings:
    """arq 可发现的 worker 设置。"""

    functions = [
        func(
            _observed(run_ddl_job),
            keep_result=0,
            timeout=app_config.redis.worker_job_timeout_seconds,
            max_tries=3,
        )
    ]
    cron_jobs = [
        cron(
            _observed(dispatch_pending),
            second={0, 10, 20, 30, 40, 50},
            run_at_startup=True,
        ),
        cron(_observed(expire_waiting), minute=None, second=0),
        cron(_observed(expire_memories), minute=None, second=1),
        cron(_observed(reap_stalled_jobs), minute=None, second=3),
        cron(
            _observed(cleanup_checkpoints),
            second={5, 15, 25, 35, 45, 55},
        ),
        cron(
            _observed(dispatch_memory_index_outbox),
            second={2, 12, 22, 32, 42, 52},
            run_at_startup=True,
        ),
        cron(
            _observed(extract_conversation_memory),
            second={4, 14, 24, 34, 44, 54},
            run_at_startup=True,
        ),
        cron(
            _observed(purge_user_memories),
            second={7, 17, 27, 37, 47, 57},
        ),
    ]
    on_startup = _observed(startup)
    on_shutdown = _observed(shutdown)
    # arq 在未提供 redis_pool 时才用 redis_settings 自建连接；这里直接提供带
    # 读取超时的队列客户端，避免半开连接让 worker 永久停在队列轮询上。
    redis_pool = _queue_pool()
    # 仍然保留 redis_settings：官方 `arq ... --check` 健康探针只读它、不读
    # redis_pool，缺省时会去连 localhost:6379/0 而不是配置指向的队列。
    redis_settings = RedisSettings.from_dsn(app_config.redis.url)
    max_jobs = app_config.redis.worker_concurrency
    job_timeout = app_config.redis.worker_job_timeout_seconds
    retry_jobs = True
    keep_result = 0
