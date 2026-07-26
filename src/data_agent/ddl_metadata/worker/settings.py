"""arq worker 发现设置。"""

from collections.abc import Callable
from typing import Any

from arq import cron, func
from arq.connections import ArqRedis, RedisSettings

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
    report_memory_index_dead_letters,
)
from data_agent.infrastructure.job_queue import (
    build_queue_pool,
    worker_connect_retries,
)
from data_agent.logging import logging_boundary
from data_agent.settings import app_config


def _observed(function: Callable[..., Any]) -> Callable[..., Any]:
    """在 arq 注册 seam 外部织入零配置日志边界。"""
    return logging_boundary()(function)


def _queue_pool() -> ArqRedis:
    """构造 worker 侧的 arq 队列客户端。

    构造逻辑与 API 共用 `build_queue_pool`，避免其中一侧补了超时另一侧漏掉。worker
    这一侧用满 `create_pool` 的启动重试预算：arq 的首条 Redis 命令早于 `on_startup`，
    连接失败没有任何兜底，进程会直接退出并带走全部维护 cron。

    Returns:
        已设置 arq 运行期属性的队列客户端。
    """
    return build_queue_pool(connect_retries=worker_connect_retries())


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
        # 死信告警独立成低频 cron：挂在 dispatch 上时，队列持续饱和会让
        # "批次未满才统计"永不成立，积压完全没有信号。
        cron(_observed(report_memory_index_dead_letters), minute=None, second=9),
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
